"""SQLite storage for N1 investigation playbooks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


class PlaybookNotFoundError(KeyError):
    """Raised when a requested playbook id does not exist."""


class PlaybookStore:
    """Persist playbooks for one Logan user."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS playbooks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_process_session_id TEXT NOT NULL,
                    since_ts TEXT NOT NULL,
                    until_ts TEXT NOT NULL,
                    warning TEXT NOT NULL,
                    steps_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save(self, playbook: Dict[str, Any]) -> Dict[str, Any]:
        steps_json = json.dumps(playbook["steps"], separators=(",", ":"))
        window = playbook["window"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO playbooks (
                    id, name, description, owner, created_at,
                    source_process_session_id, since_ts, until_ts,
                    warning, steps_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playbook["id"],
                    playbook["name"],
                    playbook.get("description", ""),
                    playbook["owner"],
                    playbook["created_at"],
                    playbook["source_process_session_id"],
                    window["since"],
                    window["until"],
                    playbook.get("warning", ""),
                    steps_json,
                ),
            )
            conn.commit()
        return dict(playbook)

    def get(self, playbook_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM playbooks WHERE id = ?",
                (playbook_id,),
            ).fetchone()
        if row is None:
            raise PlaybookNotFoundError(playbook_id)
        return self._row_to_playbook(row)

    def list(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, description, owner, created_at,
                       source_process_session_id, since_ts, until_ts,
                       warning, steps_json
                FROM playbooks
                ORDER BY created_at DESC
                """
            ).fetchall()

        items = []
        for row in rows:
            steps = json.loads(row["steps_json"])
            items.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "owner": row["owner"],
                    "created_at": row["created_at"],
                    "source_process_session_id": row["source_process_session_id"],
                    "window": {"since": row["since_ts"], "until": row["until_ts"]},
                    "step_count": len(steps),
                    "warning": row["warning"],
                }
            )
        return items

    def delete(self, playbook_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM playbooks WHERE id = ?", (playbook_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_playbook(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "owner": row["owner"],
            "created_at": row["created_at"],
            "source_process_session_id": row["source_process_session_id"],
            "window": {"since": row["since_ts"], "until": row["until_ts"]},
            "steps": json.loads(row["steps_json"]),
            "warning": row["warning"],
        }
