from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.persistence.db import _DAO
from app.urls import canonical_product_url

def _canonical_product_key(value: str) -> str:
    """Normalize equivalent product URLs before indexing reusable knowledge."""
    return canonical_product_url(value)

class KnowledgeDAO(_DAO):
    """Domain persistence for knowledge."""

    def upsert_knowledge(
        self, product_key: str, evidence: dict[str, Any], confidence: float
    ) -> None:
        product_key = _canonical_product_key(product_key)
        now = datetime.now(UTC).isoformat()
        row = self.connection.execute(
            "SELECT version FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        version = (row["version"] + 1) if row else 1
        if row:
            existing = self.connection.execute(
                "SELECT evidence_json FROM product_knowledge WHERE product_key=?", (product_key,)
            ).fetchone()
            existing_evidence = json.loads(existing["evidence_json"]) if existing else {}
            # Discovery refreshes route/DOM evidence, whereas successful action
            # evidence is accumulated after production execution. Do not discard
            # that cache merely because a discovery payload does not contain it.
            if "successful_actions" in existing_evidence and "successful_actions" not in evidence:
                evidence = {
                    **evidence,
                    "successful_actions": existing_evidence["successful_actions"],
                }
        evidence_payload = json.dumps(evidence)
        self.connection.execute(
            "INSERT INTO product_knowledge(id, product_key, version, evidence_json, confidence, last_verified_at) VALUES(?,?,?,?,?,?) ON CONFLICT(product_key) DO UPDATE SET version=excluded.version,evidence_json=excluded.evidence_json,confidence=excluded.confidence,last_verified_at=excluded.last_verified_at",
            (str(uuid4()), product_key, version, evidence_payload, confidence, now),
        )
        # Keep an immutable version ledger alongside the current snapshot. This
        # makes freshness/re-grounding auditable without overwriting the only
        # copy of older product knowledge.
        fingerprint = str(
            evidence.get("product_fingerprint")
            or evidence.get("fingerprint")
            or hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()[:32]
        )
        self.connection.execute(
            "INSERT INTO knowledge_versions(id, product_key, version, fingerprint, evidence_json, confidence, captured_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(product_key, version) DO UPDATE SET fingerprint=excluded.fingerprint,evidence_json=excluded.evidence_json,confidence=excluded.confidence,captured_at=excluded.captured_at",
            (str(uuid4()), product_key, version, fingerprint, evidence_payload, confidence, now),
        )
        self.connection.commit()

    def upsert_page_knowledge(
        self, product_key: str, pages: list[dict[str, Any]], *, confidence: float
    ) -> None:
        """Persist page-level knowledge separately from the product snapshot.

        Run artifacts remain the immutable audit record. This table is the
        reusable, freshness-stamped index used only after discovery has
        re-grounded it on the live product.
        """
        product_key = _canonical_product_key(product_key)
        product = self.connection.execute(
            "SELECT id FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        if not product:
            raise KeyError(f"product knowledge has not been created: {product_key}")
        now = datetime.now(UTC).isoformat()
        for page in pages:
            url = _canonical_product_key(str(page.get("url", "")).strip())
            if not url:
                continue
            self.connection.execute(
                """INSERT INTO page_knowledge(id, product_knowledge_id, url, evidence_json, confidence, last_verified_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(product_knowledge_id, url) DO UPDATE SET
                  evidence_json=excluded.evidence_json, confidence=excluded.confidence,
                  last_verified_at=excluded.last_verified_at""",
                (str(uuid4()), product["id"], url, json.dumps(page), confidence, now),
            )
        self.connection.commit()

    def record_successful_actions(self, product_key: str, events: list[dict[str, Any]]) -> None:
        """Cache compact, non-secret action evidence for later DOM re-grounding."""
        product_key = _canonical_product_key(product_key)
        row = self.connection.execute(
            "SELECT evidence_json, confidence FROM product_knowledge WHERE product_key=?",
            (product_key,),
        ).fetchone()
        if not row:
            return
        evidence = json.loads(row["evidence_json"])
        cached = list(evidence.get("successful_actions", []))
        by_signature = {
            (
                item.get("kind"),
                item.get("target", {}).get("selector"),
                item.get("target", {}).get("name"),
            ): item
            for item in cached
        }
        for event in events:
            target = event.get("target") or {}
            if not event.get("success") or not target:
                continue
            # Values, before/after state, screenshots and URLs can contain user
            # data. A cache only needs semantic target identity and operation kind.
            item = {
                "kind": event.get("kind"),
                "intent": event.get("intent", ""),
                "target": {
                    key: target[key]
                    for key in ("name", "selector", "role", "test_id", "text")
                    if target.get(key) is not None
                },
            }
            signature = (item["kind"], item["target"].get("selector"), item["target"].get("name"))
            by_signature[signature] = item
        evidence["successful_actions"] = list(by_signature.values())[-60:]
        self.upsert_knowledge(product_key, evidence, float(row["confidence"]))

    def fresh_knowledge(
        self, product_key: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        product_key = _canonical_product_key(product_key)
        row = self._fetchone(
            "SELECT evidence_json, confidence, last_verified_at FROM product_knowledge WHERE product_key=?",
            (product_key,),
        )
        if not row:
            return None
        verified = datetime.fromisoformat(row["last_verified_at"])
        if (datetime.now(UTC) - verified).total_seconds() > max_age_seconds:
            return None
        return {
            "evidence": json.loads(row["evidence_json"]),
            "confidence": row["confidence"],
            "last_verified_at": row["last_verified_at"],
            "version": self._fetchone(
                "SELECT version FROM product_knowledge WHERE product_key=?", (product_key,)
            )["version"],
        }

    def knowledge_versions(self, product_key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """List immutable knowledge snapshots newest-first for audit/reuse decisions."""
        product_key = _canonical_product_key(product_key)
        rows = self.connection.execute(
            "SELECT id, product_key, version, fingerprint, confidence, captured_at FROM knowledge_versions WHERE product_key=? ORDER BY version DESC LIMIT ?",
            (product_key, max(1, min(100, int(limit)))),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_understanding_preview(
        self, product_key: str, prompt: str, payload: dict[str, Any]
    ) -> None:
        """Persist a non-secret preflight result for prompt assistance reuse."""
        product_key = _canonical_product_key(product_key)
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """INSERT INTO understanding_previews(id, product_key, prompt_hash, payload_json, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(product_key, prompt_hash) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
            (str(uuid4()), product_key, prompt_hash, json.dumps(payload), now, now),
        )
        self.connection.commit()

    def fresh_understanding_preview(
        self, product_key: str, prompt: str, max_age_seconds: int = 86_400
    ) -> dict[str, Any] | None:
        """Return a recent preflight payload, or None when it needs re-grounding."""
        product_key = _canonical_product_key(product_key)
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()
        row = self.connection.execute(
            "SELECT payload_json, updated_at FROM understanding_previews WHERE product_key=? AND prompt_hash=?",
            (product_key, prompt_hash),
        ).fetchone()
        if not row:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        if (datetime.now(UTC) - updated).total_seconds() > max_age_seconds:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def invalidate_knowledge(self, product_key: str) -> bool:
        """Remove a reusable product snapshot and all page snapshots atomically.

        Immutable run artifacts remain untouched; the next discovery is therefore
        forced to re-ground live knowledge rather than reusing stale cache data.
        """
        product_key = _canonical_product_key(product_key)
        row = self.connection.execute(
            "SELECT id FROM product_knowledge WHERE product_key=?", (product_key,)
        ).fetchone()
        if not row:
            return False
        self.connection.execute(
            "DELETE FROM page_knowledge WHERE product_knowledge_id=?", (row["id"],)
        )
        self.connection.execute("DELETE FROM product_knowledge WHERE id=?", (row["id"],))
        self.connection.commit()
        return True
