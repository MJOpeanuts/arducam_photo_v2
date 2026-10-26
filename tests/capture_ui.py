"""Render the UI without hardware; see README for the offscreen screen configuration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox, QPushButton, QScrollArea

from arducam_photo_v2 import app as ui
from arducam_photo_v2.banks import BankStore
from arducam_photo_v2.config import Bank, CameraDevice


def sample_image():
    image = np.full((720, 1280, 3), (209, 216, 220), dtype=np.uint8)
    for x in range(1280):
        image[:, x] = np.clip(image[:, x].astype(int) - x // 22, 0, 255)
    cv2.rectangle(image, (0, 470), (1280, 720), (151, 163, 173), -1)
    cv2.ellipse(image, (640, 548), (360, 40), 0, 0, 360, (120, 132, 142), -1)
    for x, height, color in ((310, 270, (91, 116, 139)), (470, 330, (112, 143, 155)),
                              (630, 240, (140, 166, 163)), (790, 290, (134, 131, 183))):
        cv2.rectangle(image, (x, 540 - height), (x + 130, 540), color, -1)
        cv2.rectangle(image, (x + 8, 548 - height), (x + 122, 560 - height), (217, 220, 218), -1)
    cv2.circle(image, (980, 470), 75, (95, 107, 120), -1)
    cv2.putText(image, "IMAGE SIMULEE - aucune camera reelle", (330, 650),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 49, 58), 1, cv2.LINE_AA)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--output", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--states", action="store_true")
    parser.add_argument("--allow-scroll", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyleSheet(ui.APP_STYLE)
    screen = app.primaryScreen().availableGeometry()
    assert screen.width() >= args.width and screen.height() >= args.height, (
        "Use QT_QPA_PLATFORM=offscreen:configfile=tests/qt_offscreen.json")
    report = []

    def save(widget, name, *, check_buttons=True):
        app.processEvents()
        if widget is window:
            assert (widget.width(), widget.height()) == (args.width, args.height)
        positions = [(area, area.verticalScrollBar().value()) for area in widget.findChildren(QScrollArea)]
        if check_buttons:
            for button in widget.findChildren(QPushButton):
                if button.isVisible():
                    assert button.width() >= button.sizeHint().width(), (name, button.text())
                    assert button.height() >= 48 and button.width() >= 48, (name, button.text())
                    if args.allow_scroll and button.visibleRegion().boundingRect().size() != button.size():
                        parent = button.parentWidget()
                        while parent is not None and not isinstance(parent, QScrollArea):
                            parent = parent.parentWidget()
                        if parent is not None:
                            parent.ensureWidgetVisible(button)
                            app.processEvents()
                    assert button.visibleRegion().boundingRect().size() == button.size(), (name, button.text())
        for area, position in positions:
            area.verticalScrollBar().setValue(position)
        app.processEvents()
        for label in widget.findChildren(QLabel):
            if label.isVisible() and label.wordWrap() and label.text():
                assert label.height() >= label.heightForWidth(label.width()), (
                    name, label.text(), label.size(), label.heightForWidth(label.width()))
        ratio = widget.devicePixelRatioF()
        suffix = "" if args.width == 1280 else f"-{args.width}"
        if ratio != 1:
            suffix += f"-{round(ratio * 100)}"
        path = args.output / f"{name}{suffix}.png"
        shot = widget.grab()
        assert shot.save(str(path))
        report.append({"file": str(path), "logical": [widget.width(), widget.height()],
                       "pixels": [shot.width(), shot.height()], "scale": ratio,
                       "screen_logical": [screen.width(), screen.height()]})

    device = CameraDevice(ui.DEFAULT_BACKEND, 0, "Arducam B0494C", "simulation-only")
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.object(ui, "list_devices", return_value=[device]), \
                patch.object(ui, "default_photo_dir", return_value=root / "photos"), \
                patch.object(ui.MainWindow, "_ensure_preview"):
            store = BankStore(root / "banks.sqlite3")
            bank = store.create(Bank.draft("Planche couleur", "color_4k", 512, device))
            window = ui.MainWindow(store)
            window.work_bank = window.saved_bank = bank
            window.session_device = device
            window._capture_bank_id = bank.id
            window.reload_banks()
            window.refresh_work_controls()
            window._focus_report = {"state": "deferred", "value": 512}
            window._focus_sync()
            window.photo_path_label.setText("Images / ArducamPhotoV2")
            window.preview_details.append("Démonstration hors caméra — aucun accès matériel.\n"
                                          "Les rapports techniques restent accessibles ici.")
            window.resize(args.width, args.height)
            window.show()
            app.processEvents()
            image = sample_image()
            window.preview_image.set_array(image)
            window.preview_status.setText("Aperçu simulé — aucune caméra réelle.")
            save(window, "preview")
            window.tabs.setCurrentIndex(1)
            window.capture_image.set_array(image)
            window.capture_state.setText("Image de démonstration — aucune acquisition réelle.")
            save(window, "capture")
            window.tabs.setCurrentIndex(2)
            window._source = image
            window._source_raw = False
            window._populate_operations()
            window.source_label.set_array(image)
            result = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            window.result_label.set_array(result)
            window.save_result_button.setEnabled(True)
            window.source_info.setText("Image de démonstration · couleur 1280 × 720")
            window.processing_status.setText("Comparaison simulée · source inchangée · caméra inactive.")
            save(window, "traitement")

            if args.states:
                window.tabs.setCurrentIndex(0)
                window.preview_panel.setCurrentIndex(1)
                save(window, "diagnostic")
                window.preview_panel.setCurrentIndex(0)
                window._set_preview_message(
                    "Erreur d’aperçu : caméra débranchée. Utilisez « Relancer le Preview » dans Diagnostic.")
                save(window, "preview-error")
                window.tabs.setCurrentIndex(1)
                QTest.mousePress(window.photo_button, Qt.MouseButton.LeftButton)
                save(window, "capture-pressed")
                with patch.object(window, "_start_task"):
                    QTest.mouseRelease(window.photo_button, Qt.MouseButton.LeftButton)
                assert window._busy_capture and not window.photo_button.isEnabled()
                save(window, "capture-busy")
                window._capture_finished()

                def error_dialog(parent, title, message):
                    dialog = QMessageBox(QMessageBox.Icon.Critical, title, message,
                                         QMessageBox.StandardButton.Ok, parent)
                    dialog.show()
                    context = "capture" if title == "Capture impossible" else "traitement"
                    save(dialog, f"{context}-error-dialog")
                    save(window, f"{context}-error")
                    dialog.close()

                with patch.object(QMessageBox, "critical", side_effect=error_dialog):
                    window._capture_failure(
                        "Étape stabilisation : 30 images non conformes. Capture interrompue ; "
                        "aucun changement de résolution ni de backend.")
                QTimer.singleShot(0, lambda: (
                    save(app.activeModalWidget(), "capture-diagnostic"), app.activeModalWidget().accept()))
                window.show_diagnostic()
                for numeric, name in ((True, "focus-dialog"), (False, "name-dialog")):
                    dialog = ui.TouchKeyboard("Focus manuel" if numeric else "Nom de la banque",
                                              "512" if numeric else bank.name, numeric, window)
                    dialog.show()
                    save(dialog, name)
                    dialog.close()
                dialog = QFileDialog(window, "Ouvrir une acquisition ou une image", directory,
                                     "Acquisition / images (*.json *.png *.jpg *.jpeg)")
                dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
                dialog.show()
                save(dialog, "file-dialog", check_buttons=False)
                dialog.close()
                window.work_bank = Bank.draft("Modification non sauvegardée", "color_4k", 600, device)

                def dismiss_changes():
                    dialog = app.activeModalWidget()
                    save(dialog, "unsaved-dialog")
                    dialog.reject()

                QTimer.singleShot(0, dismiss_changes)
                window._resolve_dirty()
                window.work_bank = window.saved_bank
                window.tabs.setCurrentIndex(2)
                with patch.object(QMessageBox, "critical", side_effect=error_dialog):
                    window._source_failed("Fichier illisible : image absente ou format non pris en charge.",
                                          window._source_generation)
                window.tabs.setCurrentIndex(0)
                ui.GATE.claim("preview")
                ui.GATE.release("Libération refusée par le pilote (simulation).")
                window._enter_fault()
                save(window, "camera-fault")
                ui.GATE.reset_for_tests()
            window.hide()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
