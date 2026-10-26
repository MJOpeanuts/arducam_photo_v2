from dataclasses import replace

import numpy as np
import pytest

from arducam_photo_v2 import camera
from arducam_photo_v2.config import Bank


class FakeCV:
    CAP_MSMF = 1
    CAP_DSHOW = 2
    CAP_ANY = 3
    CAP_PROP_FRAME_WIDTH = 10
    CAP_PROP_FRAME_HEIGHT = 11
    CAP_PROP_CONVERT_RGB = 12
    CAP_PROP_FOCUS = 13


class FakeCap:
    def __init__(self, frames, reject=None):
        self.frames = list(frames)
        self.reject = reject
        self.values = {}
        self.set_calls = []
        self.released = 0

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.set_calls.append((prop, value))
        self.values[prop] = value
        return prop != self.reject

    def get(self, prop):
        return self.values.get(prop, 0)

    def read(self):
        return True, self.frames.pop(0)

    def release(self):
        self.released += 1


@pytest.fixture
def small_camera(monkeypatch):
    monkeypatch.setattr(camera, "_cv2", lambda: FakeCV)
    monkeypatch.setattr(camera, "RAW_BYTES", 108)
    monkeypatch.setattr(camera, "RAW_TRANSPORT", (6, 9))
    monkeypatch.setattr(camera, "RAW_IMAGE_SHAPE", (9, 12))
    for key, mode in list(camera.MODES.items()):
        size = (12, 8) if key != "raw_108mp" else (12, 9)
        monkeypatch.setitem(camera.MODES, key, replace(mode, width=size[0], height=size[1]))


@pytest.mark.parametrize("mode,shape", [
    ("color_720p", (8, 12, 3)), ("color_4k", (8, 12, 3)),
    ("color_12mp", (8, 12, 3)), ("raw_108mp", (9, 12)),
])
def test_all_modes_are_explicit_and_camera_released(small_camera, mode, shape):
    frame = np.full((108,), 120, np.uint8) if mode == "raw_108mp" else np.zeros(shape, np.uint8)
    cap = FakeCap([frame] * 6)
    bank = Bank.draft("Mode", mode, 300)
    result = camera.acquire(bank, "capture-id", opener=lambda *_: cap)
    assert result.original.shape == shape
    assert result.original.dtype == np.uint8
    assert result.released and cap.released == 1
    assert result.focus_requested == 300 and result.frames_read == 6
    assert result.settings["focus"]["requested"] == 300
    assert set(call[0] for call in cap.set_calls).issubset({
        FakeCV.CAP_PROP_FRAME_WIDTH, FakeCV.CAP_PROP_FRAME_HEIGHT,
        FakeCV.CAP_PROP_CONVERT_RGB, FakeCV.CAP_PROP_FOCUS,
    })
    if mode == "raw_108mp":
        assert (FakeCV.CAP_PROP_FRAME_WIDTH, 6) in cap.set_calls
        assert (FakeCV.CAP_PROP_FRAME_HEIGHT, 9) in cap.set_calls
        assert (FakeCV.CAP_PROP_CONVERT_RGB, 0) in cap.set_calls


def test_raw_validation_rejects_wrong_type_and_size(small_camera):
    with pytest.raises(camera.CameraReadError, match="uint8"):
        camera.validate_raw(np.zeros(108, dtype=np.uint16))
    with pytest.raises(camera.CameraReadError, match="108"):
        camera.validate_raw(np.zeros(107, dtype=np.uint8))


def test_wrong_color_resolution_is_not_substituted(small_camera):
    cap = FakeCap([np.zeros((9, 12, 3), np.uint8)])
    with pytest.raises(camera.CameraReadError, match="attendus"):
        camera.acquire(Bank.draft("Couleur", "color_720p", 0), "id",
                       opener=lambda *_: cap, stabilizing_reads=0)
    assert cap.released == 1


def test_rejected_focus_fails_and_releases(small_camera):
    cap = FakeCap([], reject=FakeCV.CAP_PROP_FOCUS)
    with pytest.raises(camera.CameraSetupError, match="focus"):
        camera.acquire(Bank.draft("Refus", "color_720p", 21), "id", opener=lambda *_: cap)
    assert cap.released == 1


def test_undefined_focus_never_reaches_camera(small_camera):
    with pytest.raises(camera.CameraSetupError, match="focus"):
        camera.acquire(Bank.draft("Sans focus", "color_720p", None), "id",
                       opener=lambda *_: pytest.fail("must not open camera"))


def test_camera_owner_is_exclusive(small_camera):
    camera._CAMERA_LOCK.acquire()
    try:
        with pytest.raises(camera.CameraBusyError):
            camera.acquire(Bank.draft("Busy", "color_720p", 2), "id")
    finally:
        camera._CAMERA_LOCK.release()


def test_release_error_is_reported_without_discarding_received_pixels(small_camera):
    class ReleaseFailure(FakeCap):
        def release(self):
            self.released += 1
            raise OSError("driver release error")

    cap = ReleaseFailure([np.zeros((8, 12, 3), np.uint8)] * 6)
    result = camera.acquire(Bank.draft("Release", "color_720p", 300), "id",
                            opener=lambda *_: cap)
    assert result.original.shape == (8, 12, 3)
    assert not result.released
    assert "driver release error" in result.release_error
    assert result.settings["focus"]["requested"] == 300


def test_focus_readback_failure_does_not_misreport_accepted_command(small_camera):
    class ReadbackFailure(FakeCap):
        def get(self, prop):
            if prop == FakeCV.CAP_PROP_FOCUS:
                raise OSError("readback unavailable")
            return super().get(prop)

    cap = ReadbackFailure([np.zeros((8, 12, 3), np.uint8)] * 6)
    result = camera.acquire(Bank.draft("Readback", "color_720p", 300), "id",
                            opener=lambda *_: cap)
    assert result.settings["focus"]["accepted"] is True
    assert result.settings["focus"]["readback"] is None
    assert result.settings["focus"]["readback_error"] == "readback unavailable"
