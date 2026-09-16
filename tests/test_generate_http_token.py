"""Tests for the HTTPS bearer-token generator."""

import os
from pathlib import Path

import pytest

from scripts.generate_http_token import write_token


def test_write_token_creates_private_secret(tmp_path: Path) -> None:
    path = tmp_path / "private" / "bearer-token"

    write_token(path)

    assert path.stat().st_mode & 0o777 == 0o600
    value = path.read_text(encoding="ascii").strip()
    assert len(value) >= 43
    assert set(value) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_write_token_requires_absolute_new_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_token(Path("relative-token"))

    path = tmp_path / "token"
    write_token(path)
    first = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_token(path)
    assert path.read_bytes() == first


def test_write_token_respects_restrictive_umask(tmp_path: Path) -> None:
    previous = os.umask(0)
    try:
        path = tmp_path / "token"
        write_token(path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777 == 0o600
