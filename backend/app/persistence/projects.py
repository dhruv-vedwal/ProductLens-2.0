from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.persistence.db import _DAO

class ProjectsDAO(_DAO):
    """Domain persistence for projects."""

    def create_project(self, name: str, *, owner_id: str | None = None) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        now = datetime.now(UTC).isoformat()
        identifier = str(uuid4())
        self.connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
            (identifier, owner_id, normalized[:160], now, now),
        )
        self.connection.commit()
        return self.get_project(identifier)

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def list_projects(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT projects.*, COUNT(requests.id) AS request_count
            FROM projects LEFT JOIN demo_requests AS requests ON requests.project_id = projects.id"""
        params: tuple[Any, ...] = ()
        if owner_id is not None:
            query += " WHERE projects.owner_id=?"
            params = (owner_id,)
        query += " GROUP BY projects.id ORDER BY projects.updated_at DESC, projects.created_at DESC"
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_project_for_user(self, project_id: str, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id, user_id)
        ).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def rename_project_for_user(self, project_id: str, user_id: str, name: str) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        self.get_project_for_user(project_id, user_id)
        self.connection.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=? AND owner_id=?",
            (normalized[:160], datetime.now(UTC).isoformat(), project_id, user_id),
        )
        self.connection.commit()
        return self.get_project_for_user(project_id, user_id)

    def rename_project(self, project_id: str, name: str) -> dict[str, Any]:
        normalized = name.strip()
        if not normalized:
            raise ValueError("project name is required")
        self.get_project(project_id)
        self.connection.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=?",
            (normalized[:160], datetime.now(UTC).isoformat(), project_id),
        )
        self.connection.commit()
        return self.get_project(project_id)

    def ensure_user_project(self, user_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE owner_id=? AND name='My Product Demos' LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else self.create_project("My Product Demos", owner_id=user_id)

    def list_projects_legacy(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT projects.*, COUNT(requests.id) AS request_count
            FROM projects LEFT JOIN demo_requests AS requests ON requests.project_id = projects.id
            GROUP BY projects.id ORDER BY projects.updated_at DESC, projects.created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def ensure_local_project(self) -> dict[str, Any]:
        """Create a transparent local-only workspace for an unauthenticated studio."""
        row = self.connection.execute(
            "SELECT * FROM projects WHERE owner_id IS NULL AND name='My Product Demos' LIMIT 1"
        ).fetchone()
        return dict(row) if row else self.create_project("My Product Demos")
