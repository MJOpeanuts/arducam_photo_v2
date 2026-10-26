import os
from dataclasses import replace
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtWidgets import QScrollArea
import numpy as np

from arducam_photo_v2 import app as ui
from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_three_integrated_tabs_and_logical_touch_targets(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(ui.MainWindow, "start_preview", lambda self: None)
    monkeypatch.setattr(ui, "default_photo_dir", lambda: tmp_path / "photos")
    saved = Bank.draft("Test", "color_720p", 100)
    store = BankStore(tmp_path / "banks.sqlite3")
    store.create(saved)
    window = ui.MainWindow(store=store)
    window.work_bank = window.saved_bank = saved
    window.refresh_work_controls()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Preview", "Capture", "Traitement"
    ]
    scroll = window.preview_page.findChild(QScrollArea)
    for width, height in ((1024, 680), (1280, 720), (1600, 900)):
        window.resize(width, height)
        qapp.processEvents()
        assert scroll.horizontalScrollBar().maximum() == 0
    window.tabs.setCurrentIndex(1)
    assert window.tabs.currentIndex() == 1
    assert not window.photo_button.isEnabled()  # no capture bank has been selected
    assert min(button.minimumHeight() for button in window.findChildren(QPushButton)) >= 48
    window.close()


def test_touch_keyboard_provides_integrated_character_and_numeric_keys(qapp):
    numeric = ui.TouchKeyboard("Focus", numeric=True)
    assert numeric.findChild(type(numeric.entry)).minimumHeight() >= 48
    assert any(button.text() == "7" for button in numeric.findChildren(QPushButton))
    name = ui.TouchKeyboard("Bank")
    assert any(button.text() == "Q" for button in name.findChildren(QPushButton))
    assert any(button.text() == "Espace" for button in name.findChildren(QPushButton))
    numeric.close()
    name.close()


def test_syncing_a_bank_never_sends_focus_command(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(ui.MainWindow, "start_preview", lambda self: None)
    monkeypatch.setattr(ui, "default_photo_dir", lambda: tmp_path / "photos")
    saved = Bank.draft("Test", "color_720p", 300)
    store = BankStore(tmp_path / "banks.sqlite3")
    store.create(saved)
    window = ui.MainWindow(store=store)
    window.work_bank = window.saved_bank = saved
    window.refresh_work_controls()

    class RecordingPreview:
        def __init__(self):
            self.commands = []

        def isRunning(self):
            return True

        def set_focus(self, value):
            self.commands.append(value)

    preview = RecordingPreview()
    window.preview_worker = preview
    window._focus_sync()
    assert preview.commands == []
    window.focus_slider.setValue(301)
    window._focus_released()
    assert preview.commands == [301]
    window.preview_worker = None
    window.work_bank = window.saved_bank = replace(window.work_bank, focus=301)
    window.refresh_work_controls()
    window.close()


def test_preview_open_does_not_reapply_loaded_focus(monkeypatch):
    class CV:
        CAP_PROP_FRAME_WIDTH = 10
        CAP_PROP_FRAME_HEIGHT = 11
        CAP_PROP_FOCUS = 13

    class Cap:
        def __init__(self):
            self.calls = []
            self.worker = None

        def isOpened(self):
            return True

        def set(self, prop, value):
            self.calls.append((prop, value))
            return True

        def read(self):
            self.worker.stop()
            return True, np.zeros((720, 1280, 3), np.uint8)

        def release(self):
            pass

    bank = Bank.draft("Loaded", "color_720p", 500)
    cap = Cap()
    worker = ui.PreviewWorker("session", bank)
    cap.worker = worker
    monkeypatch.setattr(ui, "_cv2", lambda: CV)
    monkeypatch.setattr(ui, "_open", lambda *_: cap)
    worker.run()
    assert cap.calls == [(CV.CAP_PROP_FRAME_WIDTH, 1280), (CV.CAP_PROP_FRAME_HEIGHT, 720)]


def test_processing_navigation_does_not_open_camera_or_accept_stale_preview(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(ui.MainWindow, "start_preview", lambda self: None)
    monkeypatch.setattr(ui, "default_photo_dir", lambda: tmp_path / "photos")
    monkeypatch.setattr(ui, "_open", lambda *_: pytest.fail("Traitement must not open the camera"))
    saved = Bank.draft("Saved", "color_720p", 40)
    store = BankStore(tmp_path / "banks.sqlite3")
    store.create(saved)
    window = ui.MainWindow(store=store)
    window.work_bank = window.saved_bank = saved
    window.refresh_work_controls()
    window.preview_token = "current-session"
    previous = window.focus_command_status.text()
    window._focus_result("old-session", 500, {"accepted": True, "readback": 500})
    window._preview_frame("old-session", np.zeros((4, 4, 3), dtype=np.uint8))
    assert window.focus_command_status.text() == previous
    window.tabs.setCurrentIndex(2)
    assert window.tabs.currentIndex() == 2
    window.work_bank = window.saved_bank
    window.refresh_work_controls()
    window.close()
