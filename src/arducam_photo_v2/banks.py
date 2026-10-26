from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Bank
from .paths import data_dir


class BankStore:
    """Small, versioned SQLite store, isolated from earlier applications."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else data_dir() / "banks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and not tables:
                db.execute("""CREATE TABLE banks (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
                    focus INTEGER, schema_version INTEGER NOT NULL, revision INTEGER NOT NULL,
                    camera_index INTEGER NOT NULL, backend TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                db.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise ValueError(f"Schéma de banks non pris en charge : {version}.")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _decode(row) -> Bank:
        bank = Bank(**{key: row[key] for key in (
            "id", "name", "mode", "focus", "schema_version", "revision", "camera_index", "backend"
        )})
        bank.validate()
        return bank

    def list(self) -> list[Bank]:
        with self._connect() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM banks ORDER BY name COLLATE NOCASE")]

    def get(self, bank_id: str) -> Bank:
        with self._connect() as db:
            row = db.execute("SELECT * FROM banks WHERE id=?", (bank_id,)).fetchone()
        if row is None:
            raise KeyError("Cette bank n’existe plus.")
        return self._decode(row)

    def create(self, bank: Bank) -> Bank:
        bank.validate()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("INSERT INTO banks VALUES(?,?,?,?,?,?,?,?,?)",
                       (*[getattr(bank, k) for k in ("id", "name", "mode", "focus",
                          "schema_version", "revision", "camera_index", "backend")], now))
        return bank

    def save(self, bank: Bank, expected_revision: int) -> Bank:
        bank.validate()
        updated = replace(bank, revision=expected_revision + 1)
        updated.validate()
        with self._connect() as db:
            cursor = db.execute("""UPDATE banks SET name=?, mode=?, focus=?, schema_version=?,
                revision=?, camera_index=?, backend=?, updated_at=?
                WHERE id=? AND revision=?""",
                (updated.name, updated.mode, updated.focus, updated.schema_version, updated.revision,
                 updated.camera_index, updated.backend, datetime.now(timezone.utc).isoformat(),
                 updated.id, expected_revision))
            if cursor.rowcount != 1:
                raise RuntimeError("La bank a changé depuis son chargement ; rechargez-la avant de sauvegarder.")
        return updated

    def export_snapshot(self, bank: Bank) -> dict:
        return bank.snapshot()
