"""Tests for N1 playbook SQLite storage."""

import pytest

from oci_logan_mcp.playbook_store import PlaybookNotFoundError, PlaybookStore


def _playbook(name="CPU incident", created_at="2026-04-24T10:00:00+00:00"):
    return {
        "id": "pb_123",
        "name": name,
        "description": "Captured triage",
        "owner": "testuser",
        "created_at": created_at,
        "source_process_session_id": "session-a",
        "window": {
            "since": "2026-04-24T09:00:00+00:00",
            "until": "2026-04-24T10:00:00+00:00",
        },
        "steps": [
            {
                "tool": "run_query",
                "args": {"query": "*"},
                "ts": "2026-04-24T09:15:00+00:00",
                "outcome": "invoked",
            }
        ],
        "warning": "",
    }


def test_save_get_roundtrip(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")

    saved = store.save(_playbook())
    loaded = store.get(saved["id"])

    assert loaded == saved


def test_list_orders_newest_first(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    first = _playbook(name="old", created_at="2026-04-24T09:00:00+00:00")
    first["id"] = "pb_old"
    second = _playbook(name="new", created_at="2026-04-24T10:00:00+00:00")
    second["id"] = "pb_new"

    store.save(first)
    store.save(second)

    assert [p["id"] for p in store.list()] == ["pb_new", "pb_old"]
    assert store.list()[0]["step_count"] == 1
    assert "steps" not in store.list()[0]


def test_delete_removes_playbook(tmp_path):
    store = PlaybookStore(tmp_path / "playbooks.sqlite3")
    store.save(_playbook())

    assert store.delete("pb_123") is True
    assert store.delete("pb_123") is False
    with pytest.raises(PlaybookNotFoundError):
        store.get("pb_123")
