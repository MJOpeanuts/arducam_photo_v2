import json
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from arducam_photo_v2 import camera, capture, processing
from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank, CameraDevice

DEVICE = CameraDevice(camera.DEFAULT_BACKEND, 0, "Arducam", "usb#arducam")


def banks(count=3):
    return tuple(Bank.draft(f"Carte {index}", "color_720p", index, DEVICE)
                 for index in range(count))


def acquisition(bank, identifier, *, release_error=None):
    pixels = np.full((720, 1280, 3), 42, np.uint8)
    return camera.Acquisition(pixels, bank.mode, bank.focus, True, bank.focus,
                              pixels.shape, "uint8", {"attempts": 6}, {}, DEVICE,
                              identifier, release_error is None, release_error=release_error)


def test_sequence_is_frozen_and_rejects_empty_or_duplicate_banks():
    with pytest.raises(ValueError, match="au moins"):
        capture.Sequence(())
    bank = banks(1)[0]
    with pytest.raises(ValueError, match="qu’une fois"):
        capture.Sequence((bank, bank))
    with pytest.raises(ValueError, match="incomplète"):
        capture.Sequence((replace(bank, camera=None),))
    sequence = capture.Sequence((bank,), "0001", "µC")
    with pytest.raises(FrozenInstanceError):
        sequence.object_id = "other"


@pytest.mark.parametrize("count", [1, 3])
def test_exactly_one_photo_per_bank_and_archives_survive_bank_changes(tmp_path, monkeypatch, count):
    store = BankStore(tmp_path / "banks.sqlite3")
    saved = tuple(store.create(bank) for bank in banks(count))
    sequence = capture.Sequence(saved, "000012", "Carte électronique 零")
    seen = []

    def acquire(bank, identifier):
        assert camera.GATE.owner is None
        camera.GATE.claim("capture")
        seen.append((bank, identifier))
        camera.GATE.release()
        return acquisition(bank, identifier)

    archive = capture.archive_capture

    def archive_after_release(*args, **kwargs):
        assert camera.GATE.owner is None
        return archive(*args, **kwargs)

    monkeypatch.setattr(capture, "acquire", acquire)
    monkeypatch.setattr(capture, "archive_capture", archive_after_release)
    outputs = capture.execute_sequence(sequence, tmp_path / "photos",
                                      progress=lambda _: None, completed=lambda _: None)
    assert [bank for bank, _ in seen] == list(saved)
    assert len({identifier for _, identifier in seen}) == count
    before = {path: path.read_bytes() for path in (tmp_path / "photos").rglob("*") if path.is_file()}
    for bank in saved:
        updated = store.save(replace(bank, name="Renommée", mode="raw_108mp", focus=999), bank.revision)
        store.delete(updated.id)
    assert all(path.read_bytes() == payload for path, payload in before.items())
    for output, bank in zip(outputs, saved):
        metadata = json.loads(output["manifest"].read_text())
        assert metadata["bank"] == bank.snapshot()
        assert metadata["object"]["id"] == "000012"
        assert metadata["capture_id"] == output["image"].stem == output["manifest"].stem
        assert metadata["camera"]["received_format"] == "BGR uint8"
        assert metadata["captured_at"].endswith("+00:00")
        assert metadata["original"]["path"] == output["image"].name


@pytest.mark.parametrize("failure", ["read", "release", "busy", "fault", "archive"])
def test_partial_failures_continue_only_when_camera_is_safe(tmp_path, monkeypatch, failure):
    selected = banks()
    seen = []

    def acquire(bank, identifier):
        seen.append(bank)
        if bank == selected[1]:
            if failure == "read":
                raise camera.CameraReadError("USB read error, released")
            if failure == "busy":
                raise camera.CameraBusyError("owned elsewhere")
            if failure == "fault":
                raise camera.CameraFaultError("unknown hardware state")
            if failure == "release":
                camera.GATE.claim("capture")
                camera.GATE.release("release refused")
                return acquisition(bank, identifier, release_error="release refused")
        return acquisition(bank, identifier)

    archive = capture.archive_capture

    def save(acquired, bank, *args, **kwargs):
        if failure == "archive" and bank == selected[1]:
            raise OSError("disk unavailable")
        return archive(acquired, bank, *args, **kwargs)

    monkeypatch.setattr(capture, "acquire", acquire)
    monkeypatch.setattr(capture, "archive_capture", save)
    reported = []
    outputs = capture.execute_sequence(capture.Sequence(selected), tmp_path,
                                      progress=lambda _: None, completed=reported.append)
    stop = failure in ("release", "busy", "fault")
    assert seen == list(selected[:2] if stop else selected)
    assert len(reported) == 3
    assert outputs[0]["status"] == "complete" and outputs[0]["image"].exists()
    assert outputs[1]["status"] == ("release_error" if failure == "release" else "failed")
    assert outputs[2]["status"] == ("not_executed" if stop else "complete")
    if failure == "release":
        assert outputs[1]["image"].exists() and outputs[1]["manifest"].exists()


def test_raw_render_failure_is_not_an_acquisition_failure_and_next_bank_runs(tmp_path, monkeypatch):
    selected = (replace(banks(1)[0], mode="raw_108mp"), banks(1)[0])
    selected = (selected[0], replace(selected[1], id="independent-bank"))
    monkeypatch.setitem(processing.MODES, "raw_108mp",
                        replace(processing.MODES["raw_108mp"], width=12, height=9))
    monkeypatch.setattr(processing, "project_root", lambda: tmp_path / "missing-calibration")

    def acquire(bank, identifier):
        if bank.mode != "raw_108mp":
            return acquisition(bank, identifier)
        return camera.Acquisition(np.full((9, 12), 42, np.uint8), bank.mode, bank.focus,
                                  True, bank.focus, (9, 6, 2), "uint8", {}, {}, DEVICE, identifier, True)

    monkeypatch.setattr(capture, "acquire", acquire)
    outputs = capture.execute_sequence(capture.Sequence(selected), tmp_path,
                                      progress=lambda _: None, completed=lambda _: None)
    assert [output["status"] for output in outputs] == ["render_error", "complete"]
    raw = outputs[0]
    assert raw["original"].suffix == ".raw" and raw["original"].read_bytes() == bytes([42]) * 108
    assert raw["original"].stem == raw["manifest"].stem == raw["capture_id"]
    metadata = json.loads(raw["manifest"].read_text())
    assert metadata["status"] == "render_error_original_preserved"
    assert metadata["errors"][0]["stage"] == "render"
    assert metadata["original"]["width"] == 12 and metadata["original"]["height"] == 9
