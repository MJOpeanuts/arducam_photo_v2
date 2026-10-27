import json
import time
from dataclasses import replace

import cv2

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QSize, Qt, QThread, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

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
        self.terminal_error = None
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
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def choose(window, device):
    window.camera_combo.setCurrentIndex(window.camera_combo.findData(device))
    window._camera_chosen(window.camera_combo.currentIndex())


def all_commands(workers):
    return [command for worker in workers for command in worker.commands]


# ---------------------------------------------------------------- general


def test_touch_keyboard_provides_character_and_numeric_keys(qapp):
    numeric = ui.TouchKeyboard("Focus", numeric=True)
    assert numeric.findChild(type(numeric.entry)).minimumHeight() >= 48
    assert any(button.text() == "7" for button in numeric.findChildren(QPushButton))
    name = ui.TouchKeyboard("Bank")
    assert any(button.text() == "Q" for button in name.findChildren(QPushButton))
    assert any(button.text() == "Espace" for button in name.findChildren(QPushButton))
    for keyboard in (numeric, name):
        keyboard.show()
        qapp.processEvents()
        for button in keyboard.findChildren(QPushButton):
            assert button.visibleRegion().boundingRect().size() == button.size(), button.text()
            assert button.width() >= 48 and button.height() >= 48
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


def test_terminal_failure_waits_for_manual_restart_across_refresh_tabs_and_focus(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    first = env.workers[0]
    first.terminal_error = "Étape lecture : aucune image. Relancer le Preview."
    first.end()
    assert window.preview_worker is None
    assert "Étape lecture" in window.preview_status.text()
    window._dirty = lambda: False
    window.refresh_devices()
    window.tabs.setCurrentIndex(2)
    window.tabs.setCurrentIndex(0)
    window.resend_focus()
    choose(window, WEBCAM)
    assert len(env.workers) == 1
    window.restart_preview()
    assert len(env.workers) == 2 and env.workers[1].device == WEBCAM


def test_manual_restart_waits_for_finished_release(env, qapp):
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    first = env.workers[0]
    window.restart_preview()
    assert first.stopping and len(env.workers) == 1
    first.end()
    assert len(env.workers) == 2


def test_loading_msmf_bank_never_changes_backend(env, qapp, monkeypatch):
    monkeypatch.setattr(ui, "DEFAULT_BACKEND", "DSHOW")
    current = replace(ARDUCAM, backend="DSHOW", index=4)
    env.devices[:] = [current]
    saved = env.store.create(Bank.draft("Ancienne", "color_4k", 512,
                                       replace(ARDUCAM, backend="MSMF", index=1)))
    calls = []
    monkeypatch.setattr(ui, "list_devices", lambda backend: calls.append(backend) or list(env.devices))
    window = env.make()
    window._work_selected(window.work_selector.findData(saved.id))
    assert calls == ["DSHOW"] and env.workers == []
    assert window.session_device is None and "autre backend" in window.preview_status.text()
    choose(window, current)
    assert env.workers[0].device == current
    assert (window.work_bank.mode, window.work_bank.focus) == ("color_4k", 512)
    assert env.store.get(saved.id) == saved  # no silent bank migration


# ------------------------------------------------------------------ focus

def test_plus_minus_enabled_immediately_after_first_setpoint(env, qapp):
    window = env.make()
    assert window.work_bank.focus == 0
    assert not window.focus_minus.isEnabled() and window.focus_plus.isEnabled()
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
    worker.commands.clear()
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
    assert all_commands(env.workers) == [(1, 0)]


def test_loading_bank_applies_focus_to_active_preview(env, qapp):
    saved = env.store.create(Bank.draft("Planche", "color_4k", 512, ARDUCAM))
    window = env.make()
    window.refresh_devices()
    choose(window, ARDUCAM)
    worker = env.workers[0]
    worker.commands.clear()
    window.reload_banks()
    index = window.work_selector.findData(saved.id)
    window.work_selector.setCurrentIndex(index)
    window._work_selected(index)
    assert worker.commands == [(window._focus_seq, 512)] and len(env.workers) == 1
    assert window.work_bank.mode == "color_4k"
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
    first.commands.clear()
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


def test_capture_preview_reports_unreadable_file(tmp_path):
    filename = tmp_path / "invalid.png"
    filename.write_bytes(b"not a PNG")
    with pytest.raises(OSError, match="Lecture du rendu impossible"):
        ui.capture_preview(filename)


@pytest.mark.parametrize("strided", [False, True])
def test_to_qimage_owns_pixels_after_numpy_changes_and_deletion(strided):
    pixels = np.full((30, 40, 3), (20, 80, 160), np.uint8)
    source = pixels[:, ::2] if strided else pixels
    image = ui.to_qimage(source)
    pixels[:] = 0
    del source, pixels
    assert image.pixelColor(0, 0).getRgb() == (160, 80, 20, 255)


@pytest.mark.parametrize("mode", ["color_720p", "color_4k", "color_12mp", "raw_108mp"])
def test_capture_preview_decodes_full_size_saved_render(tmp_path, mode):
    descriptor = ui.MODES[mode]
    filename = tmp_path / "Rendu été 漢字.png"
    pixels = np.full((descriptor.height, descriptor.width, 3), (20, 80, 160), np.uint8)
    ok, encoded = cv2.imencode(".png", pixels)
    assert ok
    filename.write_bytes(encoded.tobytes())
    del pixels
    image = ui.capture_preview(filename)
    expected = QSize(descriptor.width, descriptor.height)
    if max(descriptor.width, descriptor.height) > 1600:
        expected = expected.scaled(QSize(1600, 1600), Qt.AspectRatioMode.KeepAspectRatio)
    assert image.size() == expected
    assert image.pixelColor(image.width() // 2, image.height() // 2).getRgb() == (160, 80, 20, 255)
    assert filename.read_bytes() == encoded.tobytes()


# ------------------------------------------------------------------ fault


# -------------------------------------------------------- preview worker

class CV:
    CAP_PROP_FRAME_WIDTH = 10
    CAP_PROP_FRAME_HEIGHT = 11
    CAP_PROP_FOCUS = 13


@pytest.fixture
def preview_devices(monkeypatch):
    monkeypatch.setattr(ui, "list_devices", lambda backend: [ARDUCAM])


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


def test_preview_worker_opens_without_focus_unless_requested(monkeypatch, preview_devices):
    cap = Cap()
    worker = ui.PreviewWorker("session", ARDUCAM)
    cap.worker = worker
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: cap)
    statuses = []
    worker.status.connect(lambda token, message: statuses.append(message))
    worker.run()
    assert cap.calls == [(CV.CAP_PROP_FRAME_WIDTH, 1280), (CV.CAP_PROP_FRAME_HEIGHT, 720)]
    assert camera.GATE.owner is None and camera.GATE.fault is None
    assert any("disponibilité : oui" in message and "Caméra résolue" in message for message in statuses)
    assert any("(720, 1280, 3)" in message for message in statuses)


@pytest.mark.parametrize("failure", ["open", "read", "invalid", "exception"])
def test_preview_terminal_errors_release_once_without_reopening(monkeypatch, preview_devices, failure):
    class BrokenCap(Cap):
        released = 0
        reads = 0

        def isOpened(self):
            return failure != "open"

        def read(self):
            self.reads += 1
            if failure == "exception":
                raise OSError("USB disconnected")
            if failure == "invalid":
                return True, np.zeros((4, 6, 3), np.uint8)
            return False, None

        def release(self):
            self.released += 1

    cap = BrokenCap()
    opened = []
    worker = ui.PreviewWorker("session", ARDUCAM)
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "open_device", lambda device: opened.append(device) or cap)
    monkeypatch.setattr(worker, "msleep", lambda ms: None)
    statuses = []
    worker.status.connect(lambda token, message: statuses.append(message))
    worker.run()
    assert opened == [ARDUCAM] and cap.released == 1
    assert camera.GATE.owner is None and camera.GATE.fault is None
    assert worker.terminal_error and "Relancer le Preview" in statuses[-1]
    assert cap.reads == (0 if failure == "open" else 1 if failure == "exception"
                         else camera.MAX_STABILIZATION_ATTEMPTS)
    if failure == "invalid":
        assert "(4, 6, 3)" in worker.terminal_error


def test_preview_reresolves_index_before_opening(monkeypatch):
    moved = replace(ARDUCAM, index=4)
    cap = Cap()
    worker = ui.PreviewWorker("session", ARDUCAM)
    cap.worker = worker
    opened = []
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "list_devices", lambda backend: [moved])
    monkeypatch.setattr(ui, "open_device", lambda device: opened.append(device) or cap)
    worker.run()
    assert opened == [moved] and worker.device == moved


def test_preview_worker_applies_requested_focus_once_tagged_with_session(monkeypatch, preview_devices):
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


def test_preview_worker_drops_commands_after_stop(monkeypatch, preview_devices):
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


def test_preview_release_error_faults_gate(monkeypatch, preview_devices):
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


def accepted_focus(window, value=412):
    window.refresh_devices()
    choose(window, ARDUCAM)
    window._commit_focus(value)
    window._focus_result(window.preview_token, window._focus_seq, value,
                         {"accepted": True, "readback": value})


def select_bank(window, bank):
    for row in range(window.bank_list.count()):
        if window.bank_list.item(row).data(Qt.ItemDataRole.UserRole) == bank.id:
            window.bank_list.setCurrentRow(row)
            return
    pytest.fail("bank not listed")


def check_bank(window, bank):
    for row in range(window.capture_selector.count()):
        item = window.capture_selector.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == bank.id:
            item.setCheckState(Qt.CheckState.Checked)
            return
    pytest.fail("capture bank not listed")


@pytest.mark.parametrize("size", [(1280, 800), (1920, 1080)])
def test_integrated_tabs_layout_and_touch_targets(env, qapp, size):
    bank = env.store.create(Bank.draft("Carte électronique", "color_12mp", 444, ARDUCAM))
    window = env.make()
    window.work_bank = window.saved_bank = bank
    window.refresh_work_controls()
    window._focus_report = {"state": "accepted", "value": 444,
                            "report": {"accepted": True, "readback": 444}}
    window._focus_sync()
    window.resize(*size)
    window.show()
    qapp.processEvents()
    assert [window.tabs.tabText(i) for i in range(3)] == ["Preview", "Banques", "Capture"]
    assert not hasattr(window, "mode_combo")
    assert not hasattr(window, "open_source")
    assert "720p" in window.preview_disclaimer.text()
    assert not window.logo.pixmap().isNull() and not window.powered_by.pixmap().isNull()
    assert not window.photo_button.icon().isNull()
    for tab in range(3):
        window.tabs.setCurrentIndex(tab)
        qapp.processEvents()
        for button in window.tabs.currentWidget().findChildren(QPushButton):
            if button.isVisible():
                assert button.width() >= button.sizeHint().width(), button.text()
                assert button.width() >= 48 and button.height() >= 48
                assert button.visibleRegion().boundingRect().size() == button.size(), button.text()
                assert 15 <= button.font().pixelSize() <= 18
        for label in window.tabs.currentWidget().findChildren(QLabel):
            if label.isVisible() and label.wordWrap() and label.text():
                assert label.height() >= label.heightForWidth(label.width()), label.text()
    window.tabs.setCurrentIndex(0)
    assert window.preview_image.width() > window.preview_panel.width()


def test_create_from_current_focus_requires_explicit_form_validation(env, monkeypatch):
    window = env.make()
    accepted_focus(window)
    snapshots = []

    def form(dialog):
        snapshots.append((dialog.name.text(), dialog.mode.currentData()))
        dialog.name.setText("Boîtier µC")
        dialog.mode.setCurrentIndex(dialog.mode.findData("raw_108mp"))
        return ui.QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.BankDialog, "exec", lambda self: ui.QDialog.DialogCode.Rejected)
    window.create_draft()
    assert env.store.list() == []
    monkeypatch.setattr(ui.BankDialog, "exec", form)
    window.create_draft()
    bank = env.store.list()[0]
    assert (bank.name, bank.focus, bank.mode, bank.camera) == ("Boîtier µC", 412, "raw_108mp", ARDUCAM)
    assert window.work_bank.mode == "color_720p"
    assert window._task is None and not window._busy_capture
    assert snapshots


@pytest.mark.parametrize("state", ["sent", "deferred", "refused", "error", "dropped"])
def test_pending_failed_or_stale_focus_cannot_be_saved(env, monkeypatch, state):
    window = env.make()
    accepted_focus(window)
    window._focus_report["state"] = state
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    monkeypatch.setattr(ui.BankDialog, "exec", lambda self: pytest.fail("form must not open"))
    window.create_draft()
    assert env.store.list() == [] and messages


def test_missing_focus_readback_requires_acknowledgement(env, monkeypatch):
    window = env.make()
    accepted_focus(window)
    window._focus_report["report"]["readback"] = None
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    assert not window._focus_can_save()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    assert window._focus_can_save()
    window._focus_report["seq"] -= 1
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    assert not window._focus_can_save()


def test_bank_creation_does_not_save_focus_changed_while_form_is_open(env, monkeypatch):
    window = env.make()
    accepted_focus(window)

    def form(dialog):
        accepted_focus(window, 900)
        return ui.QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.BankDialog, "exec", form)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    window.create_draft()
    assert env.store.list() == []


def test_bank_checkbox_whole_row_toggles_exactly_once(env, qapp):
    bank = env.store.create(Bank.draft("Carte", "color_720p", 123, ARDUCAM))
    window = env.make()
    window.tabs.setCurrentIndex(2)
    window.show()
    qapp.processEvents()
    item = window.capture_selector.item(0)
    rect = window.capture_selector.visualItemRect(item)
    for point in (rect.center(), QPoint(rect.left() + 10, rect.center().y())):
        QTest.mouseClick(window.capture_selector.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert window._capture_bank_ids == {bank.id}
        QTest.mouseClick(window.capture_selector.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert window._capture_bank_ids == set()


def test_bank_edit_duplicate_and_delete_update_capture_selection(env, monkeypatch, tmp_path):
    bank = env.store.create(Bank.draft("Carte", "color_12mp", 456, ARDUCAM))
    window = env.make()
    select_bank(window, bank)

    def form(dialog):
        dialog.name.setText("Carte renommée")
        dialog.mode.setCurrentIndex(dialog.mode.findData("color_4k"))
        return ui.QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.BankDialog, "exec", form)
    window.edit_bank()
    updated = env.store.get(bank.id)
    assert (updated.name, updated.mode, updated.revision, updated.focus) == ("Carte renommée", "color_4k", 2, 456)

    def duplicate_form(dialog):
        dialog.name.setText("Carte RAW")
        dialog.mode.setCurrentIndex(dialog.mode.findData("raw_108mp"))
        return ui.QDialog.DialogCode.Accepted

    monkeypatch.setattr(ui.BankDialog, "exec", duplicate_form)
    window.duplicate_bank()
    duplicate = next(b for b in env.store.list() if b.id != bank.id)
    assert (duplicate.mode, duplicate.focus, duplicate.revision, duplicate.camera) == ("raw_108mp", 456, 1, ARDUCAM)
    check_bank(window, updated)
    check_bank(window, duplicate)
    assert "2 banques sélectionnées = 2 photos" == window.capture_summary.text()
    old_photo = tmp_path / "photos" / "old.png"
    old_photo.parent.mkdir(exist_ok=True)
    old_photo.write_bytes(b"untouched")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Cancel)
    window.delete_bank()
    assert env.store.get(bank.id) == updated
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.delete_bank()
    assert window._capture_bank_ids == {duplicate.id}
    assert old_photo.read_bytes() == b"untouched"


def test_edit_focus_preserves_photo_mode_and_requires_explicit_save(env):
    bank = env.store.create(Bank.draft("Carte RAW", "raw_108mp", 456, ARDUCAM))
    window = env.make()
    window.tabs.setCurrentIndex(1)
    select_bank(window, bank)
    window.edit_bank_focus()
    assert window.tabs.currentIndex() == 0
    assert window.saved_bank == bank and "Carte RAW" in window.work_title.text()
    window._commit_focus(457)
    assert env.store.get(bank.id) == bank
    window._focus_result(window.preview_token, window._focus_seq, 457,
                         {"accepted": True, "readback": 457})
    assert window.save_work_bank()
    saved = env.store.get(bank.id)
    assert (saved.mode, saved.focus, saved.revision) == ("raw_108mp", 457, 2)


@pytest.mark.parametrize("choice", ["Rester", "Abandonner", "Sauvegarder"])
def test_dirty_focus_exit_has_three_explicit_choices(env, monkeypatch, choice):
    bank = env.store.create(Bank.draft("Carte", "color_12mp", 456, ARDUCAM))
    window = env.make()
    window._work_selected(window.work_selector.findData(bank.id))
    window._commit_focus(457)
    window._focus_result(window.preview_token, window._focus_seq, 457,
                         {"accepted": True, "readback": 457})
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda self: next(b for b in self.buttons() if b.text() == choice))
    window.tabs.setCurrentIndex(1)
    assert window.tabs.currentIndex() == (0 if choice == "Rester" else 1)
    assert env.store.get(bank.id).focus == (457 if choice == "Sauvegarder" else 456)
    assert window.work_bank.focus == (456 if choice == "Abandonner" else 457)


def test_focus_repeat_and_bounds(env, qapp):
    window = env.make()
    window.show()
    accepted_focus(window, 100)
    QTest.mousePress(window.focus_plus, Qt.MouseButton.LeftButton)
    QTest.qWait(720)
    QTest.mouseRelease(window.focus_plus, Qt.MouseButton.LeftButton)
    assert 102 <= window.work_bank.focus <= 106
    window._commit_focus(1023)
    assert not window.focus_plus.isEnabled()
    window.adjust_focus(1)
    assert window.work_bank.focus == 1023
    window._commit_focus(0)
    assert not window.focus_minus.isEnabled()


@pytest.mark.parametrize("object_id", ["", "000042", "µC-零件"])
def test_sequence_freezes_object_and_banks_keeps_results_in_capture(env, qapp, monkeypatch, object_id):
    from arducam_photo_v2 import capture as sequence_module
    first = env.store.create(Bank.draft("A Carte", "color_720p", 123, ARDUCAM))
    second = env.store.create(Bank.draft("B Boîtier", "color_720p", 456, ARDUCAM))
    window = env.make()
    window.tabs.setCurrentIndex(2)
    assert not window.photo_button.isEnabled()
    check_bank(window, second)
    check_bank(window, first)
    assert window._ordered_capture_banks() == (first, second)
    window.object_id.setText(object_id)
    window.object_description.setText("Composants électroniques 漢字")
    tasks = []
    monkeypatch.setattr(window, "_start_task", tasks.append)
    window.take_photo()
    window.take_photo()
    assert len(tasks) == 1
    assert not window.capture_selector.isEnabled() and not window.object_id.isEnabled()
    assert not window.choose_folder_button.isEnabled() and not window.tabs.isTabEnabled(0)
    task = tasks[0]
    window.object_id.setText("changed programmatically")
    env.store.save(replace(first, focus=999, name="Changed"), first.revision)
    env.store.delete(second.id)
    calls = []

    def fake_acquire(bank, identifier):
        calls.append(bank)
        pixels = np.full((720, 1280, 3), (20, 80, 160), np.uint8)
        return camera.Acquisition(pixels, bank.mode, bank.focus, True, bank.focus,
                                  pixels.shape, "uint8", {"attempts": 6}, {}, bank.camera,
                                  identifier, True)

    monkeypatch.setattr(sequence_module, "acquire", fake_acquire)
    progress = []
    task.signals.progress.connect(progress.append)
    task.run()
    assert calls == [first, second]
    assert progress == ["Photo 1 sur 2 — Banque A Carte", "Photo 2 sur 2 — Banque B Boîtier"]
    assert not window._busy_capture and window.tabs.currentIndex() == 2 and env.workers == []
    assert window.result_list.count() == 2 and window.capture_image.has_image()
    assert window.capture_state.text().startswith("2 réussie(s)")
    assert window.object_description.text() == "Composants électroniques 漢字"
    outputs = window._capture_results
    assert len({output["capture_id"] for output in outputs}) == 2
    assert all("preview" not in output and "thumbnail" not in output for output in outputs)
    for output, bank in zip(outputs, (first, second)):
        metadata = json.loads(output["manifest"].read_text())
        assert metadata["bank"] == bank.snapshot()
        assert metadata["object"] == {"id": object_id or None, "description": "Composants électroniques 漢字"}
        assert metadata["sequence"] == {"id": task.sequence.id, "position": output["position"], "count": 2}
        assert output["image"].stem == output["manifest"].stem == output["capture_id"]
    window.select_result(0)
    assert isinstance(tasks[-1], ui.PhotoTask)
    tasks[-1].run()
    assert window.capture_image.has_image()
    window.clear_object_information()
    assert window.object_id.text() == window.object_description.text() == ""


def test_saved_photo_display_failure_and_partial_results_are_explicit(env, monkeypatch, tmp_path):
    window = env.make()
    window.tabs.setCurrentIndex(2)
    bank = Bank.draft("Carte", "color_720p", 12, ARDUCAM)
    window._capture_success({"bank": bank, "position": 1, "status": "complete",
                             "image": tmp_path / "photo.png", "display_error": "decode error"})
    assert window.capture_image.text() == "Photo enregistrée, affichage indisponible"
    window._capture_success({"bank": bank, "position": 2, "status": "failed", "error": "USB error"})
    window._capture_success({"bank": bank, "position": 3, "status": "not_executed"})
    window._capture_finished()
    assert window.result_list.count() == 3
    assert window.capture_state.text() == "1 réussie(s) · 1 en échec · 1 non exécutée(s)."
    assert env.workers == [] and window.tabs.currentIndex() == 2


def test_capture_task_decodes_off_gui_thread(env, qapp, monkeypatch, tmp_path):
    from arducam_photo_v2 import capture as sequence_module
    bank = env.store.create(Bank.draft("Carte", "color_720p", 123, ARDUCAM))
    window = env.make()
    window.tabs.setCurrentIndex(2)
    check_bank(window, bank)

    def fake_acquire(bank, identifier):
        pixels = np.zeros((720, 1280, 3), np.uint8)
        return camera.Acquisition(pixels, bank.mode, bank.focus, True, bank.focus,
                                  pixels.shape, "uint8", {}, {}, ARDUCAM, identifier, True)

    monkeypatch.setattr(sequence_module, "acquire", fake_acquire)
    decode = ui.capture_preview
    threads = []

    def checked_decode(path):
        threads.append(QThread.currentThread() == qapp.thread())
        return decode(path)

    monkeypatch.setattr(ui, "capture_preview", checked_decode)
    installed = []
    install = window.capture_image.set_qimage

    def checked_install(image):
        installed.append(QThread.currentThread() == qapp.thread())
        install(image)

    monkeypatch.setattr(window.capture_image, "set_qimage", checked_install)
    window.take_photo()
    deadline = time.monotonic() + 10
    while (window._busy_capture or window._active_tasks) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert not window._busy_capture and not window._active_tasks
    assert threads == [False] and installed == [True]
