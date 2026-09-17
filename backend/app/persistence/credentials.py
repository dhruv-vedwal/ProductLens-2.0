from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.persistence.db import _DAO


class CredentialsDAO(_DAO):
    """Domain persistence for credentials."""

    def create_product_credential(
        self,
        *,
        owner_id: str,
        name: str,
        reference: str,
        username_ciphertext: str,
        password_ciphertext: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
        if not normalized:
            raise ValueError("credential name is required")
        now = datetime.now(UTC).isoformat()
        identifier = str(uuid4())
        try:
            self.connection.execute(
                "INSERT INTO product_credentials "
                "(id, owner_id, project_id, name, reference, username_ciphertext, "
                "password_ciphertext, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    owner_id,
                    project_id,
                    normalized[:64],
                    reference,
                    username_ciphertext,
                    password_ciphertext,
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, IntegrityError) as error:
            raise ValueError("a credential with that name already exists") from error
        self.connection.commit()
        return self.get_product_credential_for_user(identifier, owner_id)

    def get_product_credential_for_user(self, credential_id: str, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id, owner_id, project_id, name, reference, created_at, updated_at "
            "FROM product_credentials WHERE id=? AND owner_id=?",
            (credential_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError(credential_id)
        return dict(row)

    def list_product_credentials_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, owner_id, project_id, name, reference, created_at, updated_at "
            "FROM product_credentials WHERE owner_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_product_credential_secrets_by_reference(self, reference: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT reference, username_ciphertext, password_ciphertext "
            "FROM product_credentials WHERE reference=?",
            (reference,),
        ).fetchone()
        return dict(row) if row else None

    def delete_product_credential_for_user(self, credential_id: str, user_id: str) -> None:
        cursor = self.connection.execute(
            "DELETE FROM product_credentials WHERE id=? AND owner_id=?",
            (credential_id, user_id),
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(credential_id)

    def upsert_provider_config(
        self,
        *,
        provider_type: str,
        name: str,
        credential_reference: str | None,
        active: bool,
        priority: int = 100,
        settings: dict[str, Any] | None = None,
    ) -> None:
        if credential_reference and (
            credential_reference.startswith("sk-") or " " in credential_reference
        ):
            raise ValueError("provider credential must be a secret reference, not a key")
        self.connection.execute(
            """INSERT INTO provider_configs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider_type, name) DO UPDATE SET credential_reference=excluded.credential_reference,
            active=excluded.active, priority=excluded.priority, settings_json=excluded.settings_json,
            updated_at=excluded.updated_at""",
            (
                str(uuid4()),
                provider_type,
                name,
                credential_reference,
                int(active),
                priority,
                json.dumps(settings or {}),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def provider_configs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT provider_type, name, credential_reference, active, priority, settings_json, updated_at "
            "FROM provider_configs ORDER BY provider_type, priority, name"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["active"] = bool(item["active"])
            item["settings"] = json.loads(item.pop("settings_json"))
            result.append(item)
        return result
