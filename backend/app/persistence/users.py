from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.persistence.db import _DAO

class UsersDAO(_DAO):
    """Domain persistence for users."""

    def create_user(
        self, *, email: str, password_hash: str, display_name: str | None
    ) -> dict[str, Any]:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("email is required")
        now = datetime.now(UTC).isoformat()
        identifier = str(uuid4())
        try:
            self.connection.execute(
                "INSERT INTO users (id, email, display_name, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    normalized,
                    (display_name or "").strip()[:120] or None,
                    password_hash,
                    now,
                    now,
                ),
            )
        except (sqlite3.IntegrityError, IntegrityError) as error:
            raise ValueError("an account with that email already exists") from error
        self.connection.commit()
        return self.get_user(identifier)

    def get_user(self, user_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError(user_id)
        return dict(row)

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM users WHERE email=?", (email.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None

    def update_user_preferences(self, user_id: str, *, theme_preference: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE users SET theme_preference=?, updated_at=? WHERE id=?",
            (theme_preference, datetime.now(UTC).isoformat(), user_id),
        )
        self.connection.commit()
        return self.get_user(user_id)

    def create_session(self, user_id: str, expires_at: str) -> dict[str, Any]:
        identifier = str(uuid4())
        self.connection.execute(
            "INSERT INTO auth_sessions VALUES (?, ?, ?, ?, ?)",
            (identifier, user_id, expires_at, None, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return {"id": identifier, "user_id": user_id, "expires_at": expires_at}

    def active_session(self, session_id: str, user_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM auth_sessions WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at > ?",
            (session_id, user_id, datetime.now(UTC).isoformat()),
        ).fetchone()
        return row is not None

    def revoke_session(self, session_id: str, user_id: str) -> None:
        self.connection.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (datetime.now(UTC).isoformat(), session_id, user_id),
        )
        self.connection.commit()
