from dataclasses import replace

import numpy as np
import pytest

from arducam_photo_v2 import camera
from arducam_photo_v2.config import Bank, CameraDevice

ARDUCAM = CameraDevice("MSMF", 0, "Arducam B0494C", r"\\?\usb#vid_0c45&pid_6366#arducam")
WEBCAM = CameraDevice("MSMF", 1, "Integrated Webcam", r"\\?\usb#vid_1111&pid_2222#webcam")


class FakeCV:
    CAP_MSMF = 1400
    CAP_V4L2 = 200
    CAP_PROP_FRAME_WIDTH = 10
    CAP_PROP_FRAME_HEIGHT = 11
    CAP_PROP_CONVERT_RGB = 12
    CAP_PROP_FOCUS = 13


class Info:
    def __init__(self, index, name, path):
        self.index, self.name, self.path = index, name, path


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
        if not self.frames:
            return False, None
        frame = self.frames.pop(0)
        return (False, None) if frame is None else (True, frame)

    def release(self):
        self.released += 1


def enumerator_for(*devices):
    return lambda cv2, backend: [Info(d.index, d.name, d.path) for d in devices]


@pytest.fixture
def small_camera(monkeypatch):
    monkeypatch.setattr(camera, "_cv2", lambda: FakeCV)
    monkeypatch.setattr(camera, "RAW_BYTES", 108)
    monkeypatch.setattr(camera, "RAW_TRANSPORT", (6, 9))
    monkeypatch.setattr(camera, "RAW_IMAGE_SHAPE", (9, 12))
    for key, mode in list(camera.MODES.items()):
        size = (12, 8) if key != "raw_108mp" else (12, 9)
        monkeypatch.setitem(camera.MODES, key, replace(mode, width=size[0], height=size[1]))


def bank(mode="color_720p", focus=300, device=ARDUCAM):
    return Bank.draft("Test", mode, focus, device)


def good():
    return np.zeros((8, 12, 3), np.uint8)


@pytest.mark.parametrize("mode,shape", [
    ("color_720p", (8, 12, 3)), ("color_4k", (8, 12, 3)),
    ("color_12mp", (8, 12, 3)), ("raw_108mp", (9, 12)),
])
def test_all_modes_are_explicit_and_camera_released(small_camera, mode, shape):
    frame = np.full((108,), 120, np.uint8) if mode == "raw_108mp" else np.zeros(shape, np.uint8)
    cap = FakeCap([frame] * 6)
    result = camera.acquire(bank(mode), "capture-id", opener=lambda device: cap,
                            enumerator=enumerator_for(ARDUCAM))
    assert result.original.shape == shape and result.original.dtype == np.uint8
    assert result.released and cap.released == 1
    assert result.stabilization["valid_frames"] == 6 and result.stabilization["attempts"] == 6
    assert result.settings["focus"]["requested"] == 300
    assert {call[0] for call in cap.set_calls} <= {
        FakeCV.CAP_PROP_FRAME_WIDTH, FakeCV.CAP_PROP_FRAME_HEIGHT,
        FakeCV.CAP_PROP_CONVERT_RGB, FakeCV.CAP_PROP_FOCUS,
    }
    if mode == "raw_108mp":
        assert (FakeCV.CAP_PROP_FRAME_WIDTH, 6) in cap.set_calls
        assert (FakeCV.CAP_PROP_CONVERT_RGB, 0) in cap.set_calls


# --- camera choice ---------------------------------------------------------

def test_listing_keeps_names_and_hardware_paths(small_camera):
    devices = camera.list_devices("MSMF", enumerator=lambda cv2, backend: [
        Info(0, "Arducam B0494C", ARDUCAM.path), Info(1, "", None)])
    assert devices[0] == ARDUCAM
    assert devices[1] == CameraDevice("MSMF", 1, None, None)
    assert "sans nom" in devices[1].label


def test_resolution_follows_hardware_path_not_index():
    moved = replace(ARDUCAM, index=3)
    assert camera.resolve_device(ARDUCAM, [WEBCAM, moved]) == moved


def test_absent_camera_is_never_replaced_by_another():
    with pytest.raises(camera.CameraSelectionError, match="absente"):
        camera.resolve_device(ARDUCAM, [replace(WEBCAM, index=0)])


def test_ambiguous_camera_requires_selection():
    nameless = CameraDevice("MSMF", 0, "USB Camera")
    with pytest.raises(camera.CameraSelectionError, match="ambiguë"):
        camera.resolve_device(nameless, [nameless, replace(nameless, index=1)])
    with pytest.raises(camera.CameraSelectionError, match="identifiable"):
        camera.resolve_device(CameraDevice("MSMF", 0), [CameraDevice("MSMF", 0)])


def test_capture_opens_resolved_index_and_refuses_absent_device(small_camera):
    opened = []

    def opener(device):
        opened.append(device)
        return FakeCap([good()] * 6)

    moved = replace(ARDUCAM, index=2)
    result = camera.acquire(bank(), "id", opener=opener, enumerator=enumerator_for(WEBCAM, moved))
    assert opened == [moved] and result.device == moved
    with pytest.raises(camera.CameraSelectionError):
        camera.acquire(bank(), "id", opener=lambda d: pytest.fail("must not open"),
                       enumerator=enumerator_for(WEBCAM))
    assert camera.GATE.fault is None


def test_bank_without_camera_never_opens(small_camera):
    with pytest.raises(camera.CameraSelectionError):
        camera.acquire(bank(device=None), "id", opener=lambda d: pytest.fail("must not open"))


# --- stabilisation -----------------------------------------------------------

def test_transient_frames_are_rejected_then_valid_frames_kept(small_camera):
    transient = [np.zeros((4, 6, 3), np.uint8), None, np.zeros((8, 12), np.uint8)]
    final = np.full((8, 12, 3), 77, np.uint8)
    cap = FakeCap(transient + [good()] * 5 + [final])
    result = camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    stats = result.stabilization
    assert (stats["invalid_frames"], stats["failed_reads"], stats["valid_frames"]) == (2, 1, 6)
    assert stats["attempts"] == 9 and stats["max_attempts"] == camera.MAX_STABILIZATION_ATTEMPTS
    assert np.array_equal(result.original, final)
    assert result.received_shape == (8, 12, 3)


def test_invalid_frame_restarts_consecutive_count(small_camera):
    cap = FakeCap([good(), good(), np.zeros((9, 12, 3), np.uint8), good(), good(), good()])
    image, frame, stats = camera.stabilize(cap, "color_720p", required_valid=3, max_attempts=6)
    assert stats["attempts"] == 6 and stats["valid_frames"] == 5 and stats["invalid_frames"] == 1


def test_stabilization_limit_exhausted_fails_clearly_and_releases(small_camera):
    wrong = np.zeros((9, 12, 3), np.uint8)
    cap = FakeCap([wrong] * 40)
    with pytest.raises(camera.CameraStabilizationError) as caught:
        camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM),
                       max_attempts=7)
    message = str(caught.value)
    assert "7 tentatives" in message and "Aucune autre résolution" in message
    assert caught.value.stats["invalid_frames"] == 7
    assert cap.released == 1 and len(cap.frames) == 33
    assert camera.GATE.fault is None


def test_raw_validation_rejects_wrong_type_and_size(small_camera):
    with pytest.raises(camera.CameraReadError, match="uint8"):
        camera.validate_raw(np.zeros(108, dtype=np.uint16))
    with pytest.raises(camera.CameraReadError, match="108"):
        camera.validate_raw(np.zeros(107, dtype=np.uint8))


def test_read_exceptions_count_as_failed_reads(small_camera):
    class Flaky(FakeCap):
        def read(self):
            if not getattr(self, "raised", False):
                self.raised = True
                raise OSError("usb hiccup")
            return super().read()

    cap = Flaky([good()] * 6)
    result = camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    assert result.stabilization["failed_reads"] == 1
    assert result.stabilization["last_read_error"] == "usb hiccup"


# --- focus, exclusivity, release ---------------------------------------------

def test_rejected_focus_fails_and_releases(small_camera):
    cap = FakeCap([], reject=FakeCV.CAP_PROP_FOCUS)
    with pytest.raises(camera.CameraSetupError, match="[Ff]ocus"):
        camera.acquire(bank(focus=21), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    assert cap.released == 1


def test_undefined_focus_never_reaches_camera(small_camera):
    with pytest.raises(camera.CameraSetupError, match="[Ff]ocus"):
        camera.acquire(bank(focus=None), "id", opener=lambda d: pytest.fail("must not open camera"))


def test_camera_owner_is_exclusive(small_camera):
    camera.GATE.claim("preview")
    try:
        with pytest.raises(camera.CameraBusyError):
            camera.acquire(bank(), "id", opener=lambda d: pytest.fail("must not open"),
                           enumerator=enumerator_for(ARDUCAM))
    finally:
        camera.GATE.release()


def test_release_error_keeps_pixels_and_blocks_new_openings(small_camera):
    class ReleaseFailure(FakeCap):
        def release(self):
            self.released += 1
            raise OSError("driver release error")

    cap = ReleaseFailure([good()] * 6)
    result = camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    assert result.original.shape == (8, 12, 3)
    assert not result.released and "driver release error" in result.release_error
    assert "driver release error" in camera.GATE.fault
    # The software lock is free, yet no new opening is allowed.
    with pytest.raises(camera.CameraFaultError, match="Redémarrez"):
        camera.acquire(bank(), "id2", opener=lambda d: pytest.fail("must not reopen"),
                       enumerator=enumerator_for(ARDUCAM))
    with pytest.raises(camera.CameraFaultError):
        camera.GATE.claim("preview")


def test_release_error_after_failure_reports_both_and_faults(small_camera):
    class Broken(FakeCap):
        def release(self):
            raise OSError("stuck")

    cap = Broken([], reject=FakeCV.CAP_PROP_FRAME_WIDTH)
    with pytest.raises(camera.CameraFaultError, match="stuck"):
        camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    assert camera.GATE.fault


def test_focus_readback_failure_does_not_misreport_accepted_command(small_camera):
    class ReadbackFailure(FakeCap):
        def get(self, prop):
            if prop == FakeCV.CAP_PROP_FOCUS:
                raise OSError("readback unavailable")
            return super().get(prop)

    cap = ReadbackFailure([good()] * 6)
    result = camera.acquire(bank(), "id", opener=lambda d: cap, enumerator=enumerator_for(ARDUCAM))
    assert result.settings["focus"]["accepted"] is True
    assert result.settings["focus"]["readback"] is None
    assert result.settings["focus"]["readback_error"] == "readback unavailable"
