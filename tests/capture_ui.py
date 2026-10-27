"""Check and render the integrated UI with synthetic electronics, never hardware."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from arducam_photo_v2 import app as ui
from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank, CameraDevice


def sample_image():
    image = np.full((720, 1280, 3), (55, 75, 58), np.uint8)
    cv2.rectangle(image, (140, 80), (1140, 610), (51, 112, 38), -1)
    for row in range(6):
        y = 130 + row * 74
        cv2.line(image, (190, y), (1090, y), (83, 163, 91), 3)
        for column in range(10):
            x = 200 + column * 95
            cv2.circle(image, (x, y), 7, (150, 178, 191), -1)
    for x, y in ((380, 240), (750, 240), (560, 440)):
        cv2.rectangle(image, (x - 70, y - 45), (x + 70, y + 45), (37, 39, 42), -1)
        for pin in range(8):
            px = x - 60 + pin * 17
            cv2.line(image, (px, y - 58), (px, y - 45), (180, 185, 190), 6)
            cv2.line(image, (px, y + 45), (px, y + 58), (180, 185, 190), 6)
    cv2.putText(image, "CARTE SIMULEE - aucune camera reelle", (230, 675),
                cv2.FONT_HERSHEY_SIMPLEX, .9, (225, 230, 225), 2, cv2.LINE_AA)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--states", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyleSheet(ui.APP_STYLE)
    report = []

    def save(widget, name):
        app.processEvents()
        for button in widget.findChildren(QPushButton):
            if button.isVisible():
                assert button.width() >= button.sizeHint().width(), (name, button.text())
                assert button.width() >= 48 and button.height() >= 48, (name, button.text())
                assert button.visibleRegion().boundingRect().size() == button.size(), (name, button.text())
        for label in widget.findChildren(QLabel):
            if label.isVisible() and label.wordWrap() and label.text():
                assert label.height() >= label.heightForWidth(label.width()), (name, label.text())
        shot = widget.grab()
        path = args.output / f"{name}.png"
        assert shot.save(str(path))
        report.append({"file": str(path), "logical": [widget.width(), widget.height()],
                       "pixels": [shot.width(), shot.height()], "scale": widget.devicePixelRatioF()})

    device = CameraDevice(ui.DEFAULT_BACKEND, 0, "Arducam B0494C", "simulation-only")
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.object(ui, "list_devices", return_value=[device]), \
                patch.object(ui, "default_photo_dir", return_value=root / "photos"), \
                patch.object(ui.MainWindow, "_ensure_preview"):
            store = BankStore(root / "banks.sqlite3")
            first = store.create(Bank.draft("Carte assemblée", "color_12mp", 512, device))
            second = store.duplicate(first, "Carte RAW", "raw_108mp")
            window = ui.MainWindow(store)
            window.work_bank = window.saved_bank = first
            window.session_device = device
            window._capture_bank_ids = {first.id, second.id}
            window.reload_banks()
            window.refresh_work_controls()
            window._focus_report = {"state": "accepted", "value": 512,
                                    "report": {"accepted": True, "readback": 512}}
            window._focus_sync()
            window.preview_details.append("Simulation hors caméra — aucun accès matériel.")
            window.resize(args.width, args.height)
            window.show()
            app.processEvents()
            assert (window.width(), window.height()) == (args.width, args.height)
            image = ui.to_qimage(sample_image())
            thumbnail = image.scaled(128, 80, Qt.AspectRatioMode.KeepAspectRatio)
            window.preview_image.set_qimage(image)
            window.preview_status.setText("Aperçu simulé — aucune caméra réelle.")
            save(window, "preview")
            window.tabs.setCurrentIndex(1)
            save(window, "banques")
            window.tabs.setCurrentIndex(2)
            window.object_id.setText("000042")
            window.object_description.setText("Carte électronique — démonstration")
            for position, bank in enumerate((first, second), 1):
                window._capture_success({
                    "bank": bank, "position": position, "status": "complete",
                    "image": root / "simulation.png", "directory": root,
                    "preview": image, "thumbnail": thumbnail})
            window._capture_finished()
            save(window, "capture")
            if args.states:
                window._set_capture_busy(True)
                window.capture_state.setText("Photo 2 sur 2 — Banque Carte RAW (simulation)")
                save(window, "capture-busy")
                window._set_capture_busy(False)
                window.tabs.setCurrentIndex(0)
                window.preview_panel.setCurrentIndex(1)
                save(window, "diagnostic")
                window.preview_panel.setCurrentIndex(0)
                for numeric, name in ((True, "focus-dialog"), (False, "name-dialog")):
                    dialog = ui.TouchKeyboard("Focus manuel" if numeric else "Nom",
                                              "512" if numeric else first.name, numeric, window)
                    dialog.show()
                    save(dialog, name)
                    dialog.close()
                dialog = ui.BankDialog(first, "Créer une banque avec ce focus", window)
                dialog.show()
                save(dialog, "bank-dialog")
                dialog.close()
            window.hide()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
