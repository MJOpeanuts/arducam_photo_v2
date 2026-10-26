import json
from dataclasses import replace

import cv2

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from arducam_photo_v2 import app as ui
from arducam_photo_v2 import camera
from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank, CameraDevice

ARDUCAM = CameraDevice(ui.DEFAULT_BACKEND, 0, "Arducam B0494C", "usb#vid_0c45&pid_6366#arducam")
WEBCAM = CameraDevice(ui.DEFAULT_BACKEND, 1, "Integrated Webcam", "usb#vid_1111&pid_2222#webcam")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(ui.APP_STYLE)
    return app


class FakeWorker(QObject):
    """Stands in for PreviewWorker: records commands, never touches hardware."""
    frame = Signal(str, object)
    status = Signal(str, str)
    focus_result = Signal(str, int, int, object)
    finished = Signal()
    created = []

    def __init__(self, session_id, device, parent=None):
        super().__init__(parent)
        self.session_id, self.device = session_id, device
        self.commands, self.started, self._stopping = [], False, False
        FakeWorker.created.append(self)

    @property
    def stopping(self):
        return self._stopping

    def start(self):
        self.started = True

    def stop(self):
        self._stopping = True

    def set_focus(self, sequence, value):
        self.commands.append((sequence, value))

    def take_unapplied(self):
        return None

    def end(self):
        """Simulate the hardware session being released."""
        self.finished.emit()


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    FakeWorker.created = []
    devices = [ARDUCAM, WEBCAM]
    monkeypatch.setattr(ui, "PreviewWorker", FakeWorker)
    monkeypatch.setattr(ui, "list_devices", lambda backend: list(devices))
    monkeypatch.setattr(ui, "default_photo_dir", lambda: tmp_path / "photos")
    store = BankStore(tmp_path / "banks.sqlite3")
    windows = []

    def make():
        window = ui.MainWindow(store=store)
        window.resize(1280, 720)
        windows.append(window)
        return window

    yield type("Env", (), {"make": staticmethod(make), "store": store, "devices": devices,
                           "workers": FakeWorker.created})
    for window in windows:
        window._dirty = lambda: False
        window.hide()
        window.deleteLater()


def choose(window, device):
    window.camera_combo.setCurrentIndex(window.camera_combo.findData(device))
    window._camera_chosen(window.camera_combo.currentIndex())


def all_commands(workers):
    return [command for worker in workers for command in worker.commands]


# ---------------------------------------------------------------- general

def test_three_tabs_header_logo_footer_and_touch_targets(env, qapp):
    window = env.make()
    window.show()
    qapp.processEvents()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Preview", "Capture", "Traitement"]
    assert window.mode_combo.accessibleName() == "Mode d’acquisition"
    assert window.focus_slider.accessibleName() == "Focus manuel"
    assert window.save_bank_button.text() == "Sauvegarder la banque"
    assert "Banque utilisée pour la capture" in window.capture_selector.accessibleName()
    assert "720p" in window.preview_disclaimer.text()
    assert "champ de vue" in window.preview_disclaimer.text()
    assert window.logo.pixmap() is not None and not window.logo.pixmap().isNull()
    assert not window.powered_by.pixmap().isNull()
    assert not window.windowIcon().isNull()
    for button in window.findChildren(QPushButton):
        assert button.minimumHeight() >= ui.TOUCH_MIN or button.height() >= ui.TOUCH_MIN, button.text()
    assert window.preview_image.text() and not window.preview_image.has_image()


@pytest.mark.parametrize("size", [(1280, 721), (1024, 577), (853, 470)])
def test_focus_and_save_visible_without_scrolling(env, qapp, size):
    """1280×800 screen at 100/125/150 % Windows scaling, minus title bar and taskbar."""
    window = env.make()
    window.resize(*size)
    window.show()
    qapp.processEvents()
    for widget in (window.camera_combo, window.refresh_cameras_button, window.work_selector,
                   window.focus_minus, window.focus_plus, window.focus_input, window.focus_slider,
                   window.focus_resend, window.save_bank_button):
        rect = widget.visibleRegion().boundingRect()
        assert rect.size() == widget.size(), widget.objectName() or widget
    assert window.preview_image.width() > window.focus_slider.width()


def test_touch_keyboard_provides_character_and_numeric_keys(qapp):
    numeric = ui.TouchKeyboard("Focus", numeric=True)
    assert numeric.findChild(type(numeric.entry)).minimumHeight() >= 48
    assert any(button.text() == "7" for button in numeric.findChildren(QPushButton))
    name = ui.TouchKeyboard("Bank")
    assert any(button.text() == "Q" for button in name.findChildren(QPushButton))
    assert any(button.text() == "Espace" for button in name.findChildren(QPushButton))
    numeric.close()
    name.close()


# ----------------------------------------------------------------- camera

def test_no_camera_is_selected_silently(env, qapp):
    window = env.make()
    window.refresh_devices()
    assert window.session_device is None and env.workers == []
    assert window.camera_combo.currentIndex() == 0
    assert window.camera_combo.count() == 3
    assert "Arducam B0494C" in window.camera_combo.itemText(1)
    assert "Choisissez" in window.preview_status.text()


def test_explicit_choice_opens_preview_and_records_camera_in_bank(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    assert [w.device for w in env.workers] == [ARDUCAM] and env.workers[0].started
    assert window.work_bank.camera == ARDUCAM


def test_changing_camera_releases_session_before_opening_next(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    first = env.workers[0]
    choose(window, WEBCAM)
    assert first.stopping and len(env.workers) == 1   # no concurrent access
    first.end()
    assert [w.device for w in env.workers] == [ARDUCAM, WEBCAM]
    assert window.preview_worker is env.workers[1]


def test_explicit_refresh_keeps_moved_camera_and_refuses_absent_one(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    env.devices[:] = [WEBCAM, replace(ARDUCAM, index=2)]
    window.refresh_devices()
    assert window.session_device == replace(ARDUCAM, index=2)
    env.devices[:] = [WEBCAM]
    window.refresh_devices()
    assert window.session_device is None and window.camera_combo.currentIndex() == 0
    assert "absente" in window.preview_status.text()
    assert env.workers[0].stopping


# ------------------------------------------------------------------ focus

def test_plus_minus_enabled_immediately_after_first_setpoint(env, qapp):
    window = env.make()
    assert window.work_bank.focus is None
    assert not window.focus_minus.isEnabled() and not window.focus_plus.isEnabled()
    window.focus_slider.setValue(400)
    window._focus_released()
    assert window.focus_minus.isEnabled() and window.focus_plus.isEnabled()
    window.adjust_focus(1)
    assert window.work_bank.focus == 401 and window.focus_value.text() == "401"


def test_focus_slider_touch_zone_is_real(env, qapp):
    window = env.make()
    window.show()
    qapp.processEvents()
    slider = window.focus_slider
    assert slider.height() >= ui.TOUCH_MIN and slider.minimumHeight() >= ui.TOUCH_MIN
    assert slider.handle_rect().height() >= 40
    choose(window, ARDUCAM)
    worker = env.workers[0]
    # Touch near the top edge of the 48+ px zone, far from the drawn groove.
    point = QPoint(slider.width() * 3 // 4, 3)
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert worker.commands == []                        # nothing sent while pressing
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    assert 650 < window.work_bank.focus < 900
    assert [value for _, value in worker.commands] == [window.work_bank.focus]


def test_graphic_sync_never_sends_command(env, qapp):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 300, ARDUCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    window.work_bank = window.saved_bank = saved
    window.refresh_work_controls()
    window._focus_sync()
    window.reload_banks()
    window.tabs.setCurrentIndex(2)
    window.tabs.setCurrentIndex(0)
    assert all_commands(env.workers) == []


def test_loading_bank_applies_focus_to_active_preview(env, qapp):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 512, ARDUCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    worker = env.workers[0]
    window.reload_banks()
    index = window.work_selector.findData(saved.id)
    window.work_selector.setCurrentIndex(index)
    window._work_selected(index)
    assert worker.commands == [(window._focus_seq, 512)] and len(env.workers) == 1
    assert window.mode_combo.currentData() == "color_4k"
    assert window.focus_value.text() == "512"
    assert "512" in window.focus_command_status.text() and "envoyée" in window.focus_command_status.text()
    window._focus_result(worker.session_id, window._focus_seq, 512, {"accepted": True, "readback": 510})
    assert "acceptée" in window.focus_command_status.text() and "relecture 510" in window.focus_command_status.text()
    # Same value can be sent again after a refusal.
    window._focus_result(worker.session_id, window._focus_seq, 512, {"accepted": False})
    assert "refusée" in window.focus_command_status.text() and window.focus_resend.isEnabled()
    window.resend_focus()
    assert worker.commands[-1][1] == 512 and len(worker.commands) == 2


def test_loading_bank_without_preview_applies_focus_when_session_opens(env, qapp):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 77, ARDUCAM))
    env.devices[:] = []
    window = env.make()
    window.refresh_devices()
    window.reload_banks()
    index = window.work_selector.findData(saved.id)
    window._work_selected(index)
    assert env.workers == [] and "ouverture" in window.focus_command_status.text()
    env.devices[:] = [ARDUCAM]
    window.refresh_devices()   # the bank camera reappears: session opens, focus applied once
    assert len(env.workers) == 1 and env.workers[0].commands == [(window._focus_seq, 77)]


def test_loading_bank_on_other_camera_switches_session_then_applies(env, qapp):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 600, WEBCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    first = env.workers[0]
    window.reload_banks()
    window._work_selected(window.work_selector.findData(saved.id))
    assert first.stopping and first.commands == []
    first.end()
    assert env.workers[1].device == WEBCAM and env.workers[1].commands[-1][1] == 600


def test_stale_session_and_sequence_results_are_ignored(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    window.focus_slider.setValue(100)
    window._focus_released()
    old_seq = window._focus_seq
    window.focus_slider.setValue(200)
    window._focus_released()
    before = window.focus_command_status.text()
    window._focus_result("old-session", window._focus_seq, 200, {"accepted": True, "readback": 200})
    window._focus_result(window.preview_token, old_seq, 100, {"accepted": True, "readback": 100})
    assert window.focus_command_status.text() == before
    window._preview_frame("old-session", np.zeros((4, 4, 3), np.uint8))
    assert not window.preview_image.has_image()


# ------------------------------------------------------------------ banks

def test_saved_bank_is_selected_in_capture_and_kept_across_tabs(env, qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    other = env.store.create(Bank.draft("Autre", "color_720p", 10, WEBCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    window.focus_slider.setValue(333)
    window._focus_released()
    assert window.save_work_bank()
    saved_id = window.saved_bank.id
    window.tabs.setCurrentIndex(1)
    assert env.workers[0].stopping and not window.photo_button.isEnabled()
    env.workers[0].end()           # PHOTO only once the Preview session is released
    assert window.capture_selector.currentData() == saved_id
    summary = window.capture_summary.text()
    assert "Nouvelle banque" in summary and "333" in summary and "Arducam" in summary
    assert "Focus manuel" in summary and "color_720p" in summary
    assert window.photo_button.isEnabled() and len(env.workers) == 1
    window.reload_banks()
    window.tabs.setCurrentIndex(2)
    window.tabs.setCurrentIndex(1)
    assert window.capture_selector.currentData() == saved_id
    assert window.work_selector.currentData() == saved_id
    touched = window.capture_selector.findData(other.id)
    window.capture_selector.setCurrentIndex(touched)
    window.capture_selector.activated.emit(touched)
    window.tabs.setCurrentIndex(0)
    window.tabs.setCurrentIndex(1)
    assert window.capture_selector.currentData() == other.id


def test_unsaved_changes_prompt_is_kept(env, qapp, monkeypatch):
    asked = []

    def fake_exec(box):
        asked.append([button.text() for button in box.buttons()])
        box.clickedButton = lambda: [b for b in box.buttons() if b.text() == "Rester"][0]
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window = env.make()
    window.focus_slider.setValue(5)
    window._focus_released()
    window.tabs.setCurrentIndex(1)
    assert window.tabs.currentIndex() == 0
    assert asked and {"Sauvegarder", "Abandonner", "Rester"} <= set(asked[0])


def test_capture_task_uses_exact_saved_bank_snapshot(env, qapp, monkeypatch):
    saved = env.store.create(Bank.draft("Capture exacte", "color_12mp", 701, ARDUCAM))
    window = env.make()
    window._capture_bank_id = saved.id
    window.work_bank = replace(saved, mode="raw_108mp", focus=2, camera=WEBCAM)
    captured = []

    class RecordingTask:
        def __init__(self, bank, photo_dir):
            self.bank = bank
            self.photo_dir = photo_dir
            self.signals = ui.TaskSignals()

    monkeypatch.setattr(ui, "CaptureTask", RecordingTask)
    monkeypatch.setattr(window, "_start_task", captured.append)
    window.take_photo()
    assert captured == [window._task]
    assert window._task.bank == saved
    assert (window._task.bank.mode, window._task.bank.focus, window._task.bank.camera) == (
        "color_12mp", 701, ARDUCAM)


# ------------------------------------------------------------------ fault

def test_release_fault_blocks_camera_but_keeps_processing(env, qapp, monkeypatch):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 300, ARDUCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    camera.GATE.claim("preview")
    camera.GATE.release("Libération refusée par le pilote.")
    env.workers[0].stop()
    env.workers[0].end()
    assert window.fault_banner.isVisibleTo(window) and "Redémarrez" in window.fault_banner.text()
    for widget in (window.camera_combo, window.refresh_cameras_button, window.photo_button):
        assert not widget.isEnabled()
    window.refresh_devices()
    window._camera_chosen(window.camera_combo.findData(ARDUCAM))
    assert len(env.workers) == 1                     # never reopened
    window.reload_banks()
    window._capture_bank_id = saved.id
    window.tabs.setCurrentIndex(1)
    assert not window.photo_button.isEnabled()
    window.take_photo()
    assert window._task is None
    window.tabs.setCurrentIndex(2)
    assert window.tabs.currentIndex() == 2 and window.open_source_button.isEnabled()


def test_capture_release_error_archives_pixels_and_reports(tmp_path, monkeypatch, qapp):
    bank = Bank.draft("Planche", "color_720p", 300, ARDUCAM)
    pixels = np.full((720, 1280, 3), 40, np.uint8)

    def failing_release(frozen, capture_id):
        camera.GATE.claim("capture")
        camera.GATE.release("release refused")
        return camera.Acquisition(pixels, "color_720p", 300, True, 300, pixels.shape, "uint8",
                                  {"attempts": 6}, {}, ARDUCAM, capture_id, False,
                                  release_error="release refused")

    monkeypatch.setattr(camera, "acquire", failing_release)
    task = ui.CaptureTask(bank, tmp_path)
    outputs, failures = [], []
    task.signals.success.connect(outputs.append)
    task.signals.failure.connect(failures.append)
    task.run()
    assert failures == [] and outputs[0]["release_error"] == "release refused"
    manifest = json.loads(outputs[0]["manifest"].read_text(encoding="utf-8"))
    assert manifest["status"] == "complete_with_camera_release_error"
    original = outputs[0]["directory"] / manifest["original"]["path"]
    assert np.array_equal(np.load(original) if original.suffix == ".npy" else cv2.imread(str(original)), pixels)
    assert camera.GATE.fault


# -------------------------------------------------------- preview worker

class CV:
    CAP_PROP_FRAME_WIDTH = 10
    CAP_PROP_FRAME_HEIGHT = 11
    CAP_PROP_FOCUS = 13


class Cap:
    def __init__(self, release_error=None):
        self.calls, self.worker, self.release_error = [], None, release_error

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.calls.append((prop, value))
        return True

    def get(self, prop):
        return 0

    def read(self):
        self.worker.stop()
        return True, np.zeros((720, 1280, 3), np.uint8)

    def release(self):
        if self.release_error:
            raise OSError(self.release_error)


def test_preview_worker_opens_without_focus_unless_requested(monkeypatch):
    cap = Cap()
    worker = ui.PreviewWorker("session", ARDUCAM)
    cap.worker = worker
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: cap)
    worker.run()
    assert cap.calls == [(CV.CAP_PROP_FRAME_WIDTH, 1280), (CV.CAP_PROP_FRAME_HEIGHT, 720)]
    assert camera.GATE.owner is None and camera.GATE.fault is None


def test_preview_worker_applies_requested_focus_once_tagged_with_session(monkeypatch):
    cap = Cap()
    worker = ui.PreviewWorker("session-A", ARDUCAM)
    cap.worker = worker
    results = []
    worker.focus_result.connect(lambda *args: results.append(args))
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: cap)
    worker.set_focus(1, 100)
    worker.set_focus(2, 140)          # coalesced: only the latest is sent
    worker.run()
    assert [c for c in cap.calls if c[0] == CV.CAP_PROP_FOCUS] == [(CV.CAP_PROP_FOCUS, 140)]
    assert results[0][:3] == ("session-A", 2, 140) and results[0][3]["accepted"] is True


def test_preview_worker_drops_commands_after_stop(monkeypatch):
    cap = Cap()
    worker = ui.PreviewWorker("session", ARDUCAM)
    cap.worker = worker
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: cap)
    worker.stop()
    worker.set_focus(1, 300)
    worker.run()
    assert all(c[0] != CV.CAP_PROP_FOCUS for c in cap.calls)
    assert worker.take_unapplied() == (1, 300)


def test_preview_release_error_faults_gate(monkeypatch):
    cap = Cap(release_error="device stuck")
    worker = ui.PreviewWorker("session", ARDUCAM)
    cap.worker = worker
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: cap)
    worker.run()
    assert "device stuck" in camera.GATE.fault
    blocked = ui.PreviewWorker("next", ARDUCAM)
    statuses = []
    blocked.status.connect(lambda token, message: statuses.append(message))
    monkeypatch.setattr(ui, "open_device", lambda device: pytest.fail("must not reopen"))
    blocked.run()
    assert "Redémarrez" in statuses[-1]


def test_processing_navigation_does_not_open_camera(env, qapp, monkeypatch):
    monkeypatch.setattr(ui, "open_device", lambda *_: pytest.fail("Traitement must not open the camera"))
    window = env.make()
    window.tabs.setCurrentIndex(2)
    assert window.tabs.currentIndex() == 2 and env.workers == []
