from dataclasses import replace

import pytest

from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank


def test_bank_keeps_mode_and_focus_in_saved_revision(tmp_path):
    store = BankStore(tmp_path / "banks.sqlite3")
    working = Bank.draft("Planche", "color_4k", 321)
    saved = store.create(working)
    draft = replace(saved, mode="raw_108mp", focus=322)

    reloaded = store.get(saved.id)
    assert (reloaded.mode, reloaded.focus, reloaded.revision) == ("color_4k", 321, 1)
    updated = store.save(draft, expected_revision=1)
    assert (updated.mode, updated.focus, updated.revision) == ("raw_108mp", 322, 2)
    with pytest.raises(RuntimeError, match="changé"):
        store.save(draft, expected_revision=1)


@pytest.mark.parametrize("mode,focus", [("absent", 100), ("color_720p", -1),
                                        ("color_720p", 1024), ("color_720p", True)])
def test_invalid_bank_not_accepted(mode, focus):
    bank = Bank.draft("valid", "color_720p", 0)
    with pytest.raises(ValueError):
        replace(bank, mode=mode, focus=focus).validate()


def test_undefined_focus_remains_undefined():
    bank = Bank.draft("Cadrage", "color_720p", None)
    assert bank.focus is None
    with pytest.raises(ValueError, match="focus"):
        replace(bank, focus=1024).validate()


def test_capture_snapshot_is_unchanged_by_later_bank_save(tmp_path):
    store = BankStore(tmp_path / "banks.sqlite3")
    saved = store.create(Bank.draft("Stable", "color_4k", 275))
    frozen = store.get(saved.id)
    store.save(replace(saved, mode="raw_108mp", focus=900), expected_revision=1)
    assert (frozen.mode, frozen.focus, frozen.revision) == ("color_4k", 275, 1)
