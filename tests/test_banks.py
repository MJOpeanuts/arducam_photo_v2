import sqlite3
from dataclasses import replace

import pytest

from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank, CameraDevice

DEVICE = CameraDevice("MSMF", 1, "Arducam B0494C", r"\\?\usb#vid_0c45&pid_6366#a")


def test_bank_keeps_mode_focus_and_camera_in_saved_revision(tmp_path):
    store = BankStore(tmp_path / "banks.sqlite3")
    assert store.reset_notice is None
    saved = store.create(Bank.draft("Planche", "color_4k", 321, DEVICE))
    draft = replace(saved, mode="raw_108mp", focus=322, camera=None)
    reloaded = store.get(saved.id)
    assert (reloaded.mode, reloaded.focus, reloaded.revision, reloaded.camera) == ("color_4k", 321, 1, DEVICE)
    updated = store.save(draft, expected_revision=1)
    assert (updated.mode, updated.focus, updated.revision, updated.camera) == ("raw_108mp", 322, 2, None)
    assert store.get(saved.id).camera is None
    with pytest.raises(RuntimeError, match="changé"):
        store.save(draft, expected_revision=1)


@pytest.mark.parametrize("mode,focus", [("absent", 100), ("color_720p", -1),
                                        ("color_720p", 1024), ("color_720p", True)])
def test_invalid_bank_not_accepted(mode, focus):
    bank = Bank.draft("valid", "color_720p", 0)
    with pytest.raises(ValueError):
        replace(bank, mode=mode, focus=focus).validate()


def test_invalid_camera_context_rejected():
    with pytest.raises(ValueError):
        Bank.draft("x", "color_720p", 1, CameraDevice("UNKNOWN", 0, "cam"))
    with pytest.raises(ValueError):
        Bank.draft("x", "color_720p", 1, CameraDevice("MSMF", -1, "cam"))


def test_directshow_camera_is_persisted_without_changing_bank_settings(tmp_path):
    store = BankStore(tmp_path / "banks.sqlite3")
    device = replace(DEVICE, backend="DSHOW", index=3)
    saved = store.create(Bank.draft("DirectShow", "color_4k", 321, device))
    assert store.get(saved.id) == saved


def test_undefined_focus_remains_undefined():
    bank = Bank.draft("Cadrage", "color_720p", None)
    assert bank.focus is None and bank.camera is None


def test_capture_snapshot_is_unchanged_by_later_bank_save(tmp_path):
    store = BankStore(tmp_path / "banks.sqlite3")
    saved = store.create(Bank.draft("Stable", "color_4k", 275, DEVICE))
    frozen = store.get(saved.id)
    store.save(replace(saved, mode="raw_108mp", focus=900), expected_revision=1)
    assert (frozen.mode, frozen.focus, frozen.revision) == ("color_4k", 275, 1)


def test_previous_settings_store_is_reset_without_touching_photos(tmp_path):
    path = tmp_path / "banks.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE banks (id TEXT, name TEXT, camera_index INTEGER)")
    db.execute("INSERT INTO banks VALUES ('old', 'Ancienne', 0)")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    photo = tmp_path / "photos" / "capture" / "original.raw"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"pixels")
    store = BankStore(path)
    assert "réinitialisés" in store.reset_notice
    assert store.list() == []
    assert photo.read_bytes() == b"pixels"
    assert BankStore(path).reset_notice is None


def test_corrupt_settings_store_is_reset(tmp_path):
    path = tmp_path / "banks.sqlite3"
    path.write_bytes(b"not a database at all" * 10)
    store = BankStore(path)
    assert store.reset_notice and store.list() == []
