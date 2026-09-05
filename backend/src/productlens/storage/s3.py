"""Optional S3-compatible artifact publisher.

`boto3` is intentionally imported only for configured object-storage runs so
local development does not acquire a cloud dependency or credentials.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class S3ArtifactStorage:
    def __init__(
        self, *, bucket: str, prefix: str = "productlens-runs", endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        if not bucket:
            raise ValueError("PRODUCTLENS_S3_BUCKET is required for object storage")
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - depends on deployment extra
            raise RuntimeError("Install ProductLens with the 'storage' extra to enable S3 artifact storage") from error
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name)

    def _key(self, key: str) -> str:
        return "/".join(part for part in (self.prefix, key.strip("/")) if part)

    def put(self, source: Path, key: str) -> str:
        if not source.is_file():
            raise FileNotFoundError(source)
        object_key = self._key(key)
        self.client.upload_file(str(source), self.bucket, object_key)
        return f"s3://{self.bucket}/{object_key}"

    def publish_run(self, run_root: Path) -> dict[str, object]:
        """Upload completed evidence and publish a checksum manifest last."""
        if not run_root.is_dir():
            raise FileNotFoundError(run_root)
        entries: list[dict[str, object]] = []
        # The manifest itself is uploaded separately after every immutable
        # evidence object. Including a previous manifest here would make the
        # freshly written replacement fail local checksum verification.
        for source in sorted(
            path for path in run_root.rglob("*")
            if path.is_file() and path.name != "artifact-manifest.json"
        ):
            relative = source.relative_to(run_root).as_posix()
            location = self.put(source, f"{run_root.name}/{relative}")
            entries.append({
                "path": relative,
                "location": location,
                "bytes": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            })
        manifest = {"run_id": run_root.name, "artifacts": entries}
        manifest_path = run_root / "artifact-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_location = self.put(manifest_path, f"{run_root.name}/artifact-manifest.json")
        return {**manifest, "manifest_location": manifest_location}
