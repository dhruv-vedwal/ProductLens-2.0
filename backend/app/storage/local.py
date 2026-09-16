from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


class LocalArtifactStorage:
    def __init__(self, root: Path):
        self.root = root

    def put(self, source: Path, key: str) -> Path:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return target

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def publish_run(self, run_root: Path) -> dict[str, object]:
        """Create the same immutable manifest contract used by remote storage."""
        entries = []
        # The manifest is published last and must never checksum itself; doing
        # so guarantees a mutation as soon as the new manifest replaces the
        # prior one.
        for source in sorted(
            path
            for path in run_root.rglob("*")
            if path.is_file() and path.name != "artifact-manifest.json"
        ):
            relative = source.relative_to(run_root).as_posix()
            entries.append(
                {
                    "path": relative,
                    "location": str(source),
                    "bytes": source.stat().st_size,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
        manifest = {"run_id": run_root.name, "artifacts": entries}
        path = run_root / "artifact-manifest.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return {**manifest, "manifest_location": str(path)}
