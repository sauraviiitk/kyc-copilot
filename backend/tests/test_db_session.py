"""Database bootstrap regression tests."""

from __future__ import annotations

from pathlib import Path

from app.db.session import ensure_sqlite_directory


def test_ensure_sqlite_directory_creates_missing_parent(tmp_path: Path) -> None:
    """Fresh checkouts (CI/Render) have no gitignored data/ directory."""
    database_file = tmp_path / "missing" / "nested" / "kyc.db"
    assert not database_file.parent.exists()

    ensure_sqlite_directory(f"sqlite:///{database_file.as_posix()}")

    assert database_file.parent.is_dir()


def test_ensure_sqlite_directory_ignores_memory() -> None:
    # Must not raise or create a directory named ":memory:".
    ensure_sqlite_directory("sqlite:///:memory:")
