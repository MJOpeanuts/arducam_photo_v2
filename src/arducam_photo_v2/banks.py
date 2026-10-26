from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Bank, CameraDevice
from .paths import data_dir

STORE_VERSION = 2
_COLUMNS = ("id", "name", "mode", "focus", "revision",
            "camera_backend", "camera_index", "camera_name", "camera_path")


class BankStore:
    """SQLite bank store.

    Settings are development data: any other schema is discarded and the store
    restarts empty (``reset_notice`` explains it). Captures live elsewhere and
    are never touched.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else data_dir() / "banks.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reset_notice: str | None = None
        try:
            compatible = self._is_current()
        except sqlite3.DatabaseError:
            compatible = False
        if not compatible:
            if self.path.exists():
                for suffix in ("", "-journal", "-wal", "-shm"):
                    Path(f"{self.path}{suffix}").unlink(missing_ok=True)
                self.reset_notice = ("Réglages réinitialisés : les anciennes banques de développement "
                                     "ne sont pas reprises. Les photos ne sont pas concernées.")
            self._create()

    def _is_current(self) -> bool:
        if not self.path.exists():
            return False
        db = sqlite3.connect(self.path, timeout=10)
        try:
            return db.execute("PRAGMA user_version").fetchone()[0] == STORE_VERSION
        finally:
            db.close()

    def _create(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS banks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL, focus INTEGER,
                revision INTEGER NOT NULL, camera_backend TEXT, camera_index INTEGER,
                camera_name TEXT, camera_path TEXT, updated_at TEXT NOT NULL
            )""")
            db.execute(f"PRAGMA user_version = {STORE_VERSION}")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _decode(row) -> Bank:
        camera = None
        if row["camera_backend"] is not None:
            camera = CameraDevice(row["camera_backend"], row["camera_index"],
                                  row["camera_name"], row["camera_path"])
        bank = Bank(row["id"], row["name"], row["mode"], row["focus"], camera, row["revision"])
        bank.validate()
        return bank

    @staticmethod
    def _encode(bank: Bank) -> tuple:
        camera = bank.camera
        return (bank.id, bank.name, bank.mode, bank.focus, bank.revision,
                camera.backend if camera else None, camera.index if camera else None,
                camera.name if camera else None, camera.path if camera else None)

    def list(self) -> list[Bank]:
        with self._connect() as db:
            return [self._decode(row) for row in db.execute("SELECT * FROM banks ORDER BY name COLLATE NOCASE")]

    def get(self, bank_id: str) -> Bank:
        with self._connect() as db:
            row = db.execute("SELECT * FROM banks WHERE id=?", (bank_id,)).fetchone()
        if row is None:
            raise KeyError("Cette banque n’existe plus.")
        return self._decode(row)

    def create(self, bank: Bank) -> Bank:
        bank.validate()
        with self._connect() as db:
            db.execute(f"INSERT INTO banks ({', '.join(_COLUMNS)}, updated_at) VALUES ({', '.join('?' * 10)})",
                       (*self._encode(bank), datetime.now(timezone.utc).isoformat()))
        return bank

    def save(self, bank: Bank, expected_revision: int) -> Bank:
        updated = replace(bank, revision=expected_revision + 1)
        updated.validate()
        values = self._encode(updated)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE banks SET {', '.join(f'{c}=?' for c in _COLUMNS[1:])}, updated_at=? "
                "WHERE id=? AND revision=?",
                (*values[1:], datetime.now(timezone.utc).isoformat(), updated.id, expected_revision))
            if cursor.rowcount != 1:
                raise RuntimeError("La banque a changé depuis son chargement ; rechargez-la avant de sauvegarder.")
        return updated
