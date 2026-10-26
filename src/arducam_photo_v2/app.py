from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget, QProgressBar,
)

from .banks import BankStore
from .camera import _CAMERA_LOCK, _cv2, _open, validate_color
from .config import Bank, FOCUS_MAX, FOCUS_MIN, MODES
from .paths import default_photo_dir
from .processing import archive_capture, load_source, process_color, raw_demosaic, save_processed

APP_STYLE = """
QWidget { background: #f4f5f7; color: #20252b; font-family: 'Segoe UI'; font-size: 15px; }
QMainWindow { background: #f4f5f7; }
QTabWidget::pane { border: 0; }
QTabBar::tab { min-height: 54px; min-width: 150px; padding: 5px 22px; margin-right: 6px;
  border: 0; border-bottom: 4px solid transparent; color: #66717c; font-size: 17px; font-weight: 600; }
QTabBar::tab:selected { color: #20252b; border-bottom-color: #f07832; background: #fff; }
QPushButton, QComboBox, QLineEdit, QSpinBox { min-height: 48px; padding: 4px 12px;
  border: 1px solid #d5d9de; border-radius: 9px; background: white; }
QPushButton { font-weight: 600; }
QPushButton:hover { border-color: #f07832; }
QPushButton:disabled { color: #9aa2aa; background: #e9ebed; }
QPushButton#primary { color: white; border: 0; background: #ed762e; font-size: 19px; }
QPushButton#primary:hover { background: #d9611c; }
QPushButton#photo { min-height: 68px; font-size: 25px; }
QComboBox QAbstractItemView { min-height: 42px; }
QSlider::groove:horizontal { height: 14px; background: #dce0e4; border-radius: 7px; }
QSlider::handle:horizontal { width: 48px; height: 48px; margin: -17px 0; border-radius: 24px; background: #ed762e; }
QProgressBar { min-height: 18px; border: 0; border-radius: 8px; background: #e0e4e8; }
QProgressBar::chunk { background: #ed762e; border-radius: 8px; }
QFrame#card { background: white; border: 1px solid #e2e5e8; border-radius: 14px; }
QLabel#heading { font-size: 23px; font-weight: 700; }
QLabel#muted { color: #66717c; }
QLabel#value { font-size: 27px; font-weight: 700; }
QTextEdit { border: 1px solid #d5d9de; border-radius: 8px; background: white; }
"""


def _button(text: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setMinimumHeight(52)
    if primary:
        button.setObjectName("primary")
    return button


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


class TouchKeyboard(QDialog):
    def __init__(self, title: str, initial: str = "", numeric: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.entry = QLineEdit(initial)
        self.entry.setMinimumHeight(56)
        self.entry.setFont(self.entry.font())
        layout.addWidget(self.entry)
        grid = QGridLayout()
        grid.setSpacing(6)
        rows = ["1234567890"] if numeric else ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
        for row_number, row in enumerate(rows):
            for column, character in enumerate(row):
                key = _button(character.upper() if not numeric else character)
                key.setMinimumSize(42, 48)
                key.clicked.connect(lambda checked=False, char=character: self.entry.insert(char))
                grid.addWidget(key, row_number, column)
        layout.addLayout(grid)
        controls = QHBoxLayout()
        backspace = _button("⌫")
        backspace.clicked.connect(lambda: self.entry.backspace())
        controls.addWidget(backspace)
        if not numeric:
            space = _button("Espace")
            space.clicked.connect(lambda: self.entry.insert(" "))
            controls.addWidget(space)
        ok = _button("Valider", primary=True)
        cancel = _button("Annuler")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        controls.addWidget(ok)
        controls.addWidget(cancel)
        layout.addLayout(controls)

    def value(self) -> str:
        return self.entry.text()


class PreviewWorker(QThread):
    frame = Signal(str, object)
    status = Signal(str, str)
    focus_result = Signal(str, int, object)

    def __init__(self, session_id: str, bank: Bank, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.bank = bank
        self._stop = threading.Event()
        self._focus_queue: queue.SimpleQueue[int] = queue.SimpleQueue()

    def stop(self):
        self._stop.set()

    def set_focus(self, value: int):
        self._focus_queue.put(value)

    def _apply_pending(self, cap, cv2):
        latest = None
        while not self._focus_queue.empty():
            latest = self._focus_queue.get()
        if latest is None:
            return
        try:
            accepted = bool(cap.set(cv2.CAP_PROP_FOCUS, latest))
        except Exception as exc:
            self.focus_result.emit(self.session_id, latest, {"accepted": None, "readback": None,
                                                              "error": str(exc)})
            return
        try:
            readback = cap.get(cv2.CAP_PROP_FOCUS)
            normalized_readback = (
                float(readback) if isinstance(readback, (int, float, np.number))
                and np.isfinite(readback) else None
            )
            self.focus_result.emit(self.session_id, latest,
                                   {"accepted": accepted, "readback": normalized_readback})
        except Exception as exc:
            self.focus_result.emit(self.session_id, latest, {"accepted": accepted, "readback": None,
                                                              "readback_error": str(exc)})

    def run(self):
        cap = None
        cv2 = None
        if not _CAMERA_LOCK.acquire(blocking=False):
            self.status.emit(self.session_id, "Caméra déjà utilisée — attente impossible.")
            return
        try:
            self.status.emit(self.session_id, "Ouverture de l’aperçu de cadrage…")
            cv2 = _cv2()
            cap = _open(self.bank.camera_index, self.bank.backend)
            if cap is None or not cap.isOpened():
                self.status.emit(self.session_id, "Aperçu indisponible : caméra inaccessible.")
                return
            for prop, value, label in (
                (cv2.CAP_PROP_FRAME_WIDTH, 1280, "largeur"),
                (cv2.CAP_PROP_FRAME_HEIGHT, 720, "hauteur"),
            ):
                if not cap.set(prop, value):
                    self.status.emit(self.session_id, f"Aperçu indisponible : le pilote a refusé la {label} 720p.")
                    return
            self.status.emit(self.session_id, "Aperçu de cadrage — en attente d’image.")
            frames_received = 0
            dark_state = None
            while not self._stop.is_set():
                self._apply_pending(cap, cv2)
                try:
                    ok, frame = cap.read()
                except Exception as exc:
                    self.status.emit(self.session_id, f"Erreur de lecture caméra : {exc}")
                    break
                if not ok:
                    self.status.emit(self.session_id, "Aucune image reçue — vérifiez USB et pilote.")
                    self.msleep(150)
                    continue
                try:
                    frame = validate_color(frame, "color_720p")
                except Exception as exc:
                    self.status.emit(self.session_id, f"Image d’aperçu invalide : {exc}")
                    self.msleep(100)
                    continue
                is_dark = float(np.mean(frame)) < 8
                if is_dark != dark_state:
                    self.status.emit(
                        self.session_id,
                        "Image reçue mais sombre — aucun réglage photométrique automatique."
                        if is_dark else "Image reçue — aperçu de cadrage actif.",
                    )
                    dark_state = is_dark
                elif frames_received == 0:
                    self.status.emit(self.session_id, "Images reçues — aperçu de cadrage actif.")
                frames_received += 1
                self.frame.emit(self.session_id, frame)
                self.msleep(15)
        except Exception as exc:
            self.status.emit(self.session_id, f"Erreur d’aperçu : {exc}")
        finally:
            release_error = None
            try:
                if cap is not None and cv2 is not None and not self._focus_queue.empty():
                    self._apply_pending(cap, cv2)
                if cap is not None:
                    cap.release()
            except Exception as exc:
                release_error = f"Erreur de libération caméra : {exc}"
            finally:
                _CAMERA_LOCK.release()
            self.status.emit(self.session_id, release_error or "Aperçu libéré.")


class TaskSignals(QObject):
    progress = Signal(str)
    success = Signal(object)
    failure = Signal(str)
    finished = Signal()


class CaptureTask(QRunnable):
    def __init__(self, bank: Bank, photo_dir: Path):
        super().__init__()
        self.bank = bank
        self.photo_dir = photo_dir
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            from .camera import acquire
            identifier = str(uuid.uuid4())
            self.signals.progress.emit("Ouverture caméra et configuration du mode…")
            result = acquire(self.bank, identifier)
            self.signals.progress.emit("Original archivé ; production du rendu…")
            output = archive_capture(result, self.bank, self.photo_dir)
            image = cv2.imread(str(output["image"]), cv2.IMREAD_COLOR) if output.get("image") else None
            if image is not None:
                ratio = min(1.0, 1600 / max(image.shape[:2]))
                if ratio < 1:
                    image = cv2.resize(image, (round(image.shape[1] * ratio), round(image.shape[0] * ratio)),
                                       interpolation=cv2.INTER_AREA)
                output["preview"] = image
            self.signals.success.emit(output)
        except Exception as exc:
            archive = getattr(exc, "archive", None)
            if archive is not None:
                self.signals.success.emit(archive)
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class ImageTask(QRunnable):
    def __init__(self, image: np.ndarray, raw: bool, operation: str, value: int):
        super().__init__()
        self.image, self.raw, self.operation, self.value = image, raw, operation, value
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            if self.raw:
                if self.operation == "raw-minimal":
                    result = raw_demosaic(self.image, 0)
                elif self.operation == "raw-black":
                    result = raw_demosaic(self.image, 16)
                else:
                    result = raw_demosaic(self.image, 16, reference=True)
            else:
                result = process_color(self.image, self.operation, self.value)
            self.signals.success.emit(result)
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class SourceTask(QRunnable):
    def __init__(self, filename: str):
        super().__init__()
        self.filename = filename
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            self.signals.success.emit(load_source(self.filename))
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class SaveTask(QRunnable):
    def __init__(self, image: np.ndarray, source: Path, destination: str):
        super().__init__()
        self.image = image
        self.source = source
        self.destination = destination
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            self.signals.success.emit(str(save_processed(self.image, self.source, self.destination)))
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class MainWindow(QMainWindow):
    def __init__(self, store: BankStore | None = None):
        super().__init__()
        self.setWindowTitle("Arducam Photo")
        self.setMinimumSize(1024, 680)
        self.store = store or BankStore()
        self.photo_dir = default_photo_dir()
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)
        self.work_bank = Bank.draft("Nouvelle bank", "color_720p", None)
        self.saved_bank: Bank | None = None
        self._initial_empty_draft = True
        self.preview_worker: PreviewWorker | None = None
        self.preview_token: str | None = None
        self.previous_tab = 0
        self._changing_tab = False
        self._busy_capture = False
        self._busy_save = False
        self._preview_releasing = False
        self._task: CaptureTask | ImageTask | None = None
        self._last_capture: dict | None = None
        self._source: np.ndarray | None = None
        self._result: np.ndarray | None = None
        self._source_path: Path | None = None
        self._source_raw = False
        self._comparison_minimal: np.ndarray | None = None
        self._source_generation = 0
        self._render_generation = 0
        self._active_tasks: list[QRunnable] = []
        self._closing = False
        self._resume_preview = False
        self._build()
        self.reload_banks()
        self.refresh_work_controls()
        QTimer.singleShot(0, self.start_preview)

    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 14, 22, 18)
        outer.setSpacing(12)
        header = QHBoxLayout()
        brand = QLabel("AR / PHOTO")
        brand.setStyleSheet("color:#e86f2b;font-size:19px;font-weight:800;letter-spacing:1px")
        header.addWidget(brand)
        header.addStretch(1)
        header.addWidget(QLabel("Arducam B0494C  ·  v2"))
        outer.addLayout(header)
        self.tabs = QTabWidget()
        self.preview_page = self._build_preview()
        self.capture_page = self._build_capture()
        self.processing_page = self._build_processing()
        self.tabs.addTab(self.preview_page, "Preview")
        self.tabs.addTab(self.capture_page, "Capture")
        self.tabs.addTab(self.processing_page, "Traitement")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

    def _build_preview(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(16)
        self.preview_image = QLabel("Ouverture de l’aperçu de cadrage…")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(400, 300)
        self.preview_image.setStyleSheet("background:#222a31;color:white;border-radius:14px;font-size:18px")
        layout.addWidget(self.preview_image, 3)
        panel = QWidget()
        right = QVBoxLayout(panel)
        right.setContentsMargins(0, 0, 0, 0)
        card = _card()
        form = QVBoxLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(10)
        form.addWidget(self._heading("Préparer une bank"))
        form.addWidget(QLabel("Bank de travail"))
        self.work_selector = QComboBox()
        self.work_selector.currentIndexChanged.connect(self._work_selected)
        form.addWidget(self.work_selector)
        row = QHBoxLayout()
        self.new_bank_button = _button("Nouvelle bank")
        self.new_bank_button.clicked.connect(self.create_draft)
        self.work_name_button = _button("Nommer")
        self.work_name_button.clicked.connect(self.rename_work_bank)
        row.addWidget(self.new_bank_button)
        row.addWidget(self.work_name_button)
        form.addLayout(row)
        form.addWidget(QLabel("Mode de la bank"))
        self.mode_combo = QComboBox()
        for key, descriptor in MODES.items():
            self.mode_combo.addItem(descriptor.label, key)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addWidget(self.mode_combo)
        mode_note = QLabel(
            "Aperçu de cadrage 720p ; la bank choisit le mode photo. "
            "Champ de vue et netteté peuvent varier selon le mode."
        )
        mode_note.setWordWrap(True)
        form.addWidget(mode_note)
        form.addWidget(QLabel("Focus manuel"))
        self.focus_value = QLabel("Focus non défini")
        self.focus_value.setObjectName("value")
        self.focus_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addWidget(self.focus_value)
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(FOCUS_MIN, FOCUS_MAX)
        self.focus_slider.setSingleStep(1)
        self.focus_slider.sliderMoved.connect(self._focus_moving)
        self.focus_slider.sliderReleased.connect(self._focus_released)
        self.focus_slider.valueChanged.connect(self._focus_changed)
        form.addWidget(self.focus_slider)
        controls = QHBoxLayout()
        self.focus_minus = _button("−")
        self.focus_plus = _button("+")
        self.focus_input = _button("Saisir focus")
        self.focus_minus.clicked.connect(lambda: self.adjust_focus(-1))
        self.focus_plus.clicked.connect(lambda: self.adjust_focus(1))
        self.focus_input.clicked.connect(self.enter_focus)
        controls.addWidget(self.focus_minus)
        controls.addWidget(self.focus_plus)
        controls.addWidget(self.focus_input)
        form.addLayout(controls)
        self.focus_command_status = QLabel("Aucune consigne envoyée.")
        self.focus_command_status.setObjectName("muted")
        self.focus_command_status.setWordWrap(True)
        form.addWidget(self.focus_command_status)
        note = QLabel("Ajustez jusqu’à rendre le marquage net.\nLa valeur est une consigne moteur, pas une distance.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addWidget(note)
        self.dirty_label = QLabel("Modifications non enregistrées")
        self.dirty_label.setObjectName("muted")
        form.addWidget(self.dirty_label)
        self.save_bank_button = _button("Sauvegarder la bank", primary=True)
        self.save_bank_button.clicked.connect(self.save_work_bank)
        form.addWidget(self.save_bank_button)
        self.preview_status = QLabel("Aperçu de cadrage — ouverture en cours")
        self.preview_status.setWordWrap(True)
        self.preview_status.setObjectName("muted")
        form.addWidget(self.preview_status)
        self.restart_preview_button = _button("Relancer le Preview")
        self.restart_preview_button.clicked.connect(self.restart_preview)
        form.addWidget(self.restart_preview_button)
        self.preview_details_toggle = _button("Afficher les détails techniques")
        self.preview_details_toggle.setCheckable(True)
        self.preview_details_toggle.toggled.connect(self._toggle_preview_details)
        form.addWidget(self.preview_details_toggle)
        self.preview_details = QTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setMaximumHeight(110)
        self.preview_details.setPlaceholderText("Les rapports caméra apparaîtront ici.")
        self.preview_details.document().setMaximumBlockCount(100)
        self.preview_details.hide()
        form.addWidget(self.preview_details)
        right.addWidget(card)
        right.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(320)
        scroll.setMaximumWidth(460)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 2)
        return page

    def _build_capture(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(18)
        card = _card()
        left = QVBoxLayout(card)
        left.setContentsMargins(20, 18, 20, 20)
        left.setSpacing(13)
        left.addWidget(self._heading("Capture"))
        left.addWidget(QLabel("Bank sauvegardée"))
        self.capture_selector = QComboBox()
        self.capture_selector.currentIndexChanged.connect(self._capture_bank_changed)
        left.addWidget(self.capture_selector)
        self.capture_summary = QLabel("Sélectionnez une bank complète.")
        self.capture_summary.setWordWrap(True)
        self.capture_summary.setMinimumHeight(84)
        left.addWidget(self.capture_summary)
        self.capture_reason = QLabel("")
        self.capture_reason.setObjectName("muted")
        self.capture_reason.setWordWrap(True)
        left.addWidget(self.capture_reason)
        self.preview_link = _button("Préparer une bank dans Preview")
        self.preview_link.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        left.addWidget(self.preview_link)
        self.photo_button = _button("PHOTO", primary=True)
        self.photo_button.setObjectName("photo")
        self.photo_button.clicked.connect(self.take_photo)
        left.addWidget(self.photo_button)
        self.capture_progress = QProgressBar()
        self.capture_progress.setRange(0, 0)
        self.capture_progress.hide()
        left.addWidget(self.capture_progress)
        self.capture_state = QLabel("Aucune opération en cours.")
        self.capture_state.setWordWrap(True)
        left.addWidget(self.capture_state)
        path_row = QHBoxLayout()
        self.photo_path_label = QLabel(str(self.photo_dir))
        self.photo_path_label.setWordWrap(True)
        self.choose_folder_button = _button("Dossier…")
        self.choose_folder_button.clicked.connect(self.choose_photo_dir)
        path_row.addWidget(self.photo_path_label, 1)
        path_row.addWidget(self.choose_folder_button)
        left.addLayout(path_row)
        open_row = QHBoxLayout()
        self.open_last_button = _button("Ouvrir la dernière image")
        self.open_last_button.clicked.connect(self.open_last_image)
        self.open_folder_button = _button("Ouvrir le dossier")
        self.open_folder_button.clicked.connect(self.open_photo_folder)
        open_row.addWidget(self.open_last_button)
        open_row.addWidget(self.open_folder_button)
        left.addLayout(open_row)
        layout.addWidget(card, 1)
        self.capture_image = QLabel("Aucune image capturée")
        self.capture_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_image.setMinimumSize(420, 300)
        self.capture_image.setStyleSheet("background:#222a31;color:white;border-radius:14px;font-size:18px")
        layout.addWidget(self.capture_image, 2)
        self.open_last_button.setEnabled(False)
        return page

    def _build_processing(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(self._heading("Traitement hors caméra"))
        self.open_source_button = _button("Ouvrir acquisition / image…")
        self.open_source_button.clicked.connect(self.open_source)
        top.addStretch(1)
        top.addWidget(self.open_source_button)
        layout.addLayout(top)
        self.processing_status = QLabel("Aucun traitement en cours. Aucun accès caméra depuis cet onglet.")
        self.processing_status.setObjectName("muted")
        layout.addWidget(self.processing_status)
        controls = QHBoxLayout()
        self.source_info = QLabel("Ouvrez un manifeste d’acquisition JSON, une image PNG ou JPEG.")
        self.source_info.setWordWrap(True)
        controls.addWidget(self.source_info, 2)
        self.operation_combo = QComboBox()
        self.operation_combo.currentIndexChanged.connect(self._operation_changed)
        controls.addWidget(self.operation_combo, 2)
        self.operation_value = QSpinBox()
        self.operation_value.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.operation_value.setRange(-100, 100)
        self.operation_value.setPrefix("Contraste ")
        controls.addWidget(self.operation_value)
        self.apply_button = _button("Appliquer")
        self.apply_button.clicked.connect(self.apply_processing)
        controls.addWidget(self.apply_button)
        self.save_result_button = _button("Enregistrer sous…", primary=True)
        self.save_result_button.clicked.connect(self.save_result)
        self.save_result_button.setEnabled(False)
        controls.addWidget(self.save_result_button)
        layout.addLayout(controls)
        images = QHBoxLayout()
        source_box = QVBoxLayout()
        source_box.addWidget(QLabel("SOURCE"))
        self.source_label = QLabel("Source enregistrée")
        self.source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.source_label.setMinimumSize(320, 300)
        self.source_label.setStyleSheet("background:#222a31;color:white;border-radius:14px")
        source_box.addWidget(self.source_label, 1)
        result_box = QVBoxLayout()
        self.result_heading = QLabel("RÉSULTAT")
        result_box.addWidget(self.result_heading)
        self.result_label = QLabel("Choisissez un traitement compatible.")
        self.result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_label.setMinimumSize(320, 300)
        self.result_label.setStyleSheet("background:#222a31;color:white;border-radius:14px")
        result_box.addWidget(self.result_label, 1)
        images.addLayout(source_box, 1)
        images.addLayout(result_box, 1)
        layout.addLayout(images, 1)
        return page

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("heading")
        return label

    def _toggle_preview_details(self, visible: bool):
        self.preview_details.setVisible(visible)
        self.preview_details_toggle.setText(
            "Masquer les détails techniques" if visible else "Afficher les détails techniques"
        )

    def _show_image(self, label: QLabel, image: np.ndarray):
        if image.ndim == 2:
            qimage = QImage(image.data, image.shape[1], image.shape[0], image.strides[0],
                            QImage.Format.Format_Grayscale8)
        else:
            qimage = QImage(image.data, image.shape[1], image.shape[0], image.strides[0],
                            QImage.Format.Format_BGR888)
        preview = qimage.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        label.setPixmap(QPixmap.fromImage(preview))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._source is not None:
            self._show_image(self.source_label, self._source)
        if self._result is not None:
            self._show_image(self.result_label, self._result)

    def reload_banks(self, selected_id: str | None = None):
        banks = self.store.list()
        self._saved_banks = banks
        for combo in (getattr(self, "work_selector", None), getattr(self, "capture_selector", None)):
            if combo is None:
                continue
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— Choisir —", None)
            for bank in banks:
                combo.addItem(f"{bank.name}  ·  {MODES[bank.mode].width}×{MODES[bank.mode].height}", bank.id)
            if selected_id:
                index = combo.findData(selected_id)
                combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

    def refresh_work_controls(self):
        idx = self.mode_combo.findData(self.work_bank.mode)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, idx))
        self.mode_combo.blockSignals(False)
        self._focus_sync()
        self._update_dirty()

    def _work_selected(self, index: int):
        bank_id = self.work_selector.itemData(index)
        if not bank_id:
            return
        if not self._resolve_dirty():
            self.work_selector.blockSignals(True)
            self.work_selector.setCurrentIndex(0)
            self.work_selector.blockSignals(False)
            return
        try:
            bank = self.store.get(bank_id)
        except Exception as exc:
            self._error("Chargement de la bank", exc)
            return
        self.saved_bank = bank
        self.work_bank = bank
        self._initial_empty_draft = False
        self.refresh_work_controls()

    def create_draft(self):
        if not self._resolve_dirty():
            return
        keyboard = TouchKeyboard("Nom de la bank")
        if keyboard.exec() != QDialog.DialogCode.Accepted:
            return
        name = keyboard.value().strip()
        if not name:
            QMessageBox.warning(self, "Nom manquant", "Saisissez un nom de bank.")
            return
        self.saved_bank = None
        self._initial_empty_draft = False
        self.work_bank = Bank.draft(name, self.work_bank.mode, None, self.work_bank.camera_index,
                                    self.work_bank.backend)
        self.work_selector.setCurrentIndex(0)
        self.refresh_work_controls()

    def rename_work_bank(self):
        keyboard = TouchKeyboard("Nom de la bank", self.work_bank.name)
        if keyboard.exec() == QDialog.DialogCode.Accepted:
            name = keyboard.value().strip()
            if name:
                from dataclasses import replace
                self.work_bank = replace(self.work_bank, name=name)
                self._initial_empty_draft = False
                self._update_dirty()

    def _mode_changed(self, index: int):
        if index < 0:
            return
        from dataclasses import replace
        self.work_bank = replace(self.work_bank, mode=self.mode_combo.itemData(index))
        self._initial_empty_draft = False
        self._update_dirty()

    def _focus_moving(self, value: int):
        self.focus_value.setText(str(value))

    def _focus_changed(self, value: int):
        if self.focus_slider.isSliderDown():
            self.focus_value.setText(str(value))

    def _focus_released(self):
        value = self.focus_slider.value()
        from dataclasses import replace
        self.work_bank = replace(self.work_bank, focus=value)
        self._initial_empty_draft = False
        self.focus_value.setText(str(value))
        self._update_dirty()
        if self.preview_worker and self.preview_worker.isRunning():
            self.focus_command_status.setText(f"Consigne demandée {value} — commande en attente.")
            self.preview_worker.set_focus(value)
        else:
            self.focus_command_status.setText(
                f"Consigne {value} enregistrée dans la copie de travail ; aucune commande caméra envoyée."
            )

    def _focus_sync(self):
        focus = self.work_bank.focus
        self.focus_slider.blockSignals(True)
        self.focus_slider.setValue(focus if focus is not None else 0)
        self.focus_slider.blockSignals(False)
        self.focus_value.setText(str(focus) if focus is not None else "Focus non défini")
        self.focus_command_status.setText(
            f"Consigne de bank {focus} chargée ; aucune commande envoyée."
            if focus is not None else "Aucune consigne envoyée."
        )
        self.focus_minus.setEnabled(focus is not None)
        self.focus_plus.setEnabled(focus is not None)

    def adjust_focus(self, amount: int):
        if self.work_bank.focus is not None:
            self.focus_slider.setValue(min(FOCUS_MAX, max(FOCUS_MIN, self.work_bank.focus + amount)))
            self._focus_released()

    def enter_focus(self):
        keyboard = TouchKeyboard("Consigne de focus (0–1023)",
                                 "" if self.work_bank.focus is None else str(self.work_bank.focus), numeric=True)
        if keyboard.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            value = int(keyboard.value())
            if not FOCUS_MIN <= value <= FOCUS_MAX:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Focus invalide", "Saisissez un entier compris entre 0 et 1023.")
            return
        self.focus_slider.setValue(value)
        self._focus_released()

    def _dirty(self) -> bool:
        if self.saved_bank is None:
            return not self._initial_empty_draft
        return any(getattr(self.work_bank, key) != getattr(self.saved_bank, key)
                   for key in ("name", "mode", "focus", "camera_index", "backend"))

    def _update_dirty(self):
        dirty = self._dirty()
        if dirty:
            label = "Nouvelle bank non sauvegardée" if self.saved_bank is None else "Modifications non enregistrées"
        else:
            label = "Aucune modification" if self.saved_bank is None else "Bank sauvegardée"
        self.dirty_label.setText(label)
        self.save_bank_button.setText("Créer et sauvegarder" if self.saved_bank is None else "Sauvegarder la bank")

    def _resolve_dirty(self) -> bool:
        if not self._dirty():
            return True
        choice = QMessageBox(self)
        choice.setWindowTitle("Modifications non enregistrées")
        choice.setText("Que souhaitez-vous faire de la copie de travail ?")
        save = choice.addButton("Sauvegarder", QMessageBox.ButtonRole.AcceptRole)
        discard = choice.addButton("Abandonner", QMessageBox.ButtonRole.DestructiveRole)
        stay = choice.addButton("Rester", QMessageBox.ButtonRole.RejectRole)
        choice.setDefaultButton(stay)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is stay or clicked is None:
            return False
        if clicked is save:
            return self.save_work_bank()
        if clicked is discard:
            self.work_bank = self.saved_bank or Bank.draft("Nouvelle bank", self.work_bank.mode, None)
            self._initial_empty_draft = self.saved_bank is None
            self.refresh_work_controls()
            return True
        return False

    def save_work_bank(self) -> bool:
        try:
            self.work_bank.validate()
            if self.saved_bank is None:
                saved = self.store.create(self.work_bank)
            else:
                answer = QMessageBox.question(
                    self, "Remplacer la bank sauvegardée ?",
                    f"La bank « {self.saved_bank.name} » sera remplacée (révision suivante).",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                saved = self.store.save(self.work_bank, self.saved_bank.revision)
        except Exception as exc:
            self._error("Sauvegarde impossible", exc)
            return False
        self.saved_bank = self.work_bank = saved
        self._initial_empty_draft = False
        self.reload_banks(saved.id)
        self.refresh_work_controls()
        return True

    def start_preview(self):
        if self.tabs.currentIndex() != 0 or (self.preview_worker and self.preview_worker.isRunning()):
            return
        if self._busy_capture:
            self._resume_preview = True
            self.preview_status.setText("Aperçu en attente de la fin de la capture.")
            return
        self._focus_sync()
        self._preview_releasing = False
        self.preview_token = str(uuid.uuid4())
        self.preview_worker = PreviewWorker(self.preview_token, self.work_bank)
        self.preview_worker.frame.connect(self._preview_frame)
        self.preview_worker.status.connect(self._preview_status)
        self.preview_worker.focus_result.connect(self._focus_result)
        self.preview_worker.finished.connect(self._preview_finished)
        self.preview_worker.start()

    def stop_preview(self):
        if self.preview_worker and self.preview_worker.isRunning():
            self.preview_worker.stop()

    def restart_preview(self):
        self.preview_status.setText("Libération puis réouverture de l’aperçu…")
        self.preview_token = None
        self._resume_preview = True
        self.stop_preview()
        if not self.preview_worker or not self.preview_worker.isRunning():
            self._resume_preview = False
            self.preview_worker = None
            self.start_preview()

    @Slot(str, object)
    def _preview_frame(self, token: str, frame: np.ndarray):
        if token != self.preview_token or self.tabs.currentIndex() != 0:
            return
        self._show_image(self.preview_image, frame)

    @Slot(str, str)
    def _preview_status(self, token: str, message: str):
        if token == self.preview_token:
            self.preview_status.setText(message)
            self.preview_details.append(message)

    @Slot(str, int, object)
    def _focus_result(self, token: str, value: int, report: dict):
        if token != self.preview_token:
            return
        accepted = report.get("accepted")
        if accepted is True:
            readback = report.get("readback")
            readback_label = "relecture indisponible" if readback is None else f"relecture {readback:g}"
            if report.get("readback_error"):
                readback_label += f" ({report['readback_error']})"
            self.focus_command_status.setText(
                f"Consigne demandée {value} — commande acceptée ; {readback_label}. "
                "Mise au point non vérifiée."
            )
        elif accepted is False:
            detail = "commande refusée"
            self.focus_command_status.setText(
                f"Consigne demandée {value} — {detail}. Vous pouvez la renvoyer après correction."
            )
        else:
            self.focus_command_status.setText(
                f"Consigne demandée {value} — erreur de commande : {report.get('error', 'erreur inconnue')}."
            )
        self.preview_details.append(json.dumps(report, ensure_ascii=False))

    def _preview_finished(self):
        self._preview_releasing = False
        if self.tabs.currentIndex() == 1:
            self._update_capture_bank()
        if self._resume_preview and self.tabs.currentIndex() == 0 and not self._closing:
            self._resume_preview = False
            self.preview_worker = None
            self.start_preview()
        elif self._closing:
            self._finish_close()

    def _tab_changed(self, index: int):
        old = self.previous_tab
        if self._changing_tab:
            return
        if old == 0 and index != 0 and not self._resolve_dirty():
            self._changing_tab = True
            self.tabs.setCurrentIndex(old)
            self._changing_tab = False
            return
        self.previous_tab = index
        if old == 0 and index != 0:
            self.preview_token = None
            self._resume_preview = False
            self._preview_releasing = bool(self.preview_worker and self.preview_worker.isRunning())
            self.stop_preview()
        if index == 1:
            self.reload_banks()
            self._update_capture_bank()
        elif index == 2:
            self.stop_preview()
        elif index == 0:
            if self.preview_worker and self.preview_worker.isRunning():
                self._resume_preview = True
            else:
                self.preview_worker = None
                self.start_preview()

    def _capture_bank_changed(self, _index: int):
        self._update_capture_bank()

    def _update_capture_bank(self):
        bank_id = self.capture_selector.currentData()
        try:
            bank = self.store.get(bank_id) if bank_id else None
            if bank:
                bank.validate()
            if bank and bank.focus is not None:
                descriptor = MODES[bank.mode]
                self.capture_summary.setText(
                    f"{bank.name}\n{descriptor.label}\nFocus demandé : {bank.focus} / 1023\n"
                    f"Révision {bank.revision} · caméra {bank.camera_index} · {bank.backend}"
                )
                self.capture_reason.setText("La photo utilisera cette bank sauvegardée sans la modifier.")
                self.photo_button.setEnabled(not self._busy_capture and not self._preview_releasing)
                if self._preview_releasing and not self._busy_capture:
                    self.capture_reason.setText("Libération du Preview en cours ; PHOTO sera disponible ensuite.")
            else:
                self.capture_summary.setText("Bank complète requise : identifiant, mode et consigne de focus.")
                self.capture_reason.setText("Créez ou complétez une bank dans Preview.")
                self.photo_button.setEnabled(False)
        except Exception as exc:
            self.capture_summary.setText("La bank sélectionnée est invalide.")
            self.capture_reason.setText(str(exc))
            self.photo_button.setEnabled(False)
        if self._busy_capture:
            self.photo_button.setEnabled(False)

    def take_photo(self):
        if self._preview_releasing:
            self.capture_state.setText("Attendez la libération de la caméra par le Preview.")
            return
        bank_id = self.capture_selector.currentData()
        if not bank_id:
            self._update_capture_bank()
            return
        try:
            frozen = self.store.get(bank_id)
            frozen.validate()
            if frozen.focus is None:
                raise ValueError("Cette bank ne définit pas de consigne de focus.")
            frozen = Bank(**frozen.snapshot())
        except Exception as exc:
            self._error("Bank inutilisable", exc)
            return
        self._busy_capture = True
        self.photo_button.setEnabled(False)
        self.choose_folder_button.setEnabled(False)
        self.capture_progress.show()
        self.capture_state.setText("Préparation de la capture…")
        self._task = CaptureTask(frozen, self.photo_dir)
        self._task.signals.progress.connect(self.capture_state.setText)
        self._task.signals.success.connect(self._capture_success)
        self._task.signals.failure.connect(self._capture_failure)
        self._task.signals.finished.connect(self._capture_finished)
        self._start_task(self._task)

    def _start_task(self, task: QRunnable):
        self._active_tasks.append(task)
        task.signals.finished.connect(
            lambda task=task: self._active_tasks.remove(task) if task in self._active_tasks else None
        )
        self.pool.start(task)

    @Slot(object)
    def _capture_success(self, output: dict):
        self._last_capture = output
        self.open_last_button.setEnabled(bool(output.get("image")))
        if not output.get("image"):
            self.capture_state.setText(f"Original conservé : {output['original']}")
            self.capture_image.setText("Rendu indisponible — original Bayer conservé")
            return
        readback = output.get("focus_readback")
        focus_status = ("commande focus acceptée" if output.get("focus_command_accepted")
                        else "commande focus non confirmée")
        readback_status = "relecture indisponible" if readback is None else f"relecture : {readback:g}"
        release_status = (f" · {output['release_error']}" if output.get("release_error") else "")
        self.capture_state.setText(
            f"Capture enregistrée : {output['directory']}\n"
            f"Focus demandé {output['focus_requested']} — {focus_status}, {readback_status}; "
            f"netteté non vérifiée.{release_status}"
        )
        image = output.get("preview")
        if image is not None:
            self._show_image(self.capture_image, image)

    @Slot(str)
    def _capture_failure(self, message: str):
        self.capture_state.setText("Échec de capture, d’archivage ou de rendu.")
        QMessageBox.critical(self, "Capture impossible", message)

    def _capture_finished(self):
        self._busy_capture = False
        self.capture_progress.hide()
        self.choose_folder_button.setEnabled(True)
        self._update_capture_bank()
        if self.tabs.currentIndex() == 0 and self._resume_preview:
            self._resume_preview = False
            self.start_preview()
        if self._closing:
            self._finish_close()

    def choose_photo_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Dossier de sortie des photos", str(self.photo_dir))
        if chosen:
            self.photo_dir = Path(chosen)
            self.photo_path_label.setText(str(self.photo_dir))

    def open_last_image(self):
        if self._last_capture:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_capture["image"])))

    def open_photo_folder(self):
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.photo_dir)))

    def open_source(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Ouvrir une acquisition ou une image", str(self.photo_dir),
            "Acquisition / images (*.json *.png *.jpg *.jpeg);;Tous les fichiers (*)")
        if not filename:
            return
        self._source_generation += 1
        generation = self._source_generation
        self._render_generation += 1
        self.open_source_button.setEnabled(False)
        self.processing_status.setText("Lecture du fichier enregistré… aucun accès caméra.")
        task = SourceTask(filename)
        task.signals.success.connect(lambda loaded, generation=generation:
                                     self._source_loaded(loaded, generation))
        task.signals.failure.connect(lambda message, generation=generation:
                                     self._source_failed(message, generation))
        task.signals.finished.connect(lambda: self.open_source_button.setEnabled(True))
        self._start_task(task)

    def _source_failed(self, message: str, generation: int):
        if generation == self._source_generation:
            self.processing_status.setText(f"Ouverture impossible : {message}")
            self._error("Ouverture impossible", message)

    def _source_loaded(self, loaded, generation: int):
        if generation != self._source_generation:
            return
        image, raw, source = loaded
        self._source, self._source_raw, self._source_path = image, raw, Path(source)
        self.processing_status.setText("Source chargée depuis le disque ; caméra inactive.")
        self._result = None
        self._comparison_minimal = None
        self.save_result_button.setEnabled(False)
        self.source_info.setText(
            f"{Path(source).name} · {'Bayer brut — pas une photo couleur' if raw else 'image couleur'}"
        )
        self.source_label.setText("Préparation de l’aperçu source…")
        self.result_label.setText("Choisissez un traitement compatible.")
        self._populate_operations()
        if raw:
            self.source_label.setText("Calcul du rendu minimal de comparaison…")
            task = ImageTask(image.copy(), True, "raw-minimal", 0)
            task.signals.success.connect(lambda result, generation=generation:
                                          self._minimal_source_ready(result, generation))
            task.signals.failure.connect(lambda message, generation=generation:
                                          self._minimal_source_failure(message, generation))
            self._start_task(task)
            self.apply_button.setEnabled(False)
            self.processing_status.setText("Traitement en cours…")
        else:
            self._show_image(self.source_label, image)
            self.apply_processing()

    def _minimal_source_ready(self, image: np.ndarray, generation: int):
        if generation != self._source_generation:
            return
        self._comparison_minimal = image
        self.source_info.setText(
            f"{self._source_path.name} · Rendu minimal de comparaison ; le Bayer brut n’est pas une photo couleur."
        )
        self._show_image(self.source_label, image)
        if self.operation_combo.currentData() == "raw-minimal":
            self._result = image
            self._show_image(self.result_label, image)
            self.save_result_button.setEnabled(True)
            self.processing_status.setText("Traitement terminé. La source enregistrée reste inchangée.")
            self.apply_button.setEnabled(True)
        else:
            self.apply_processing()

    def _minimal_source_failure(self, message: str, generation: int):
        if generation == self._source_generation:
            self.source_label.setText(f"Rendu minimal indisponible : {message}")
            self.apply_button.setEnabled(True)

    def _populate_operations(self):
        self.operation_combo.blockSignals(True)
        self.operation_combo.clear()
        if self._source_raw:
            self.operation_combo.addItem("Dématriçage minimal — rendu illustratif", "raw-minimal")
            self.operation_combo.addItem("Niveau noir + dématriçage", "raw-black")
            self.operation_combo.addItem("Rendu de référence + correction couleur", "raw-reference")
            self.result_heading.setText("RÉSULTAT · RENDU COULEUR")
        else:
            self.operation_combo.addItem("Niveaux de gris", "gray")
            self.operation_combo.addItem("Contraste logiciel", "contrast")
            self.operation_combo.addItem("Réduction avec proportions conservées", "reduce")
            self.result_heading.setText("RÉSULTAT")
        self.operation_combo.blockSignals(False)
        self._operation_changed()

    def _operation_changed(self, _index: int = 0):
        operation = self.operation_combo.currentData()
        enabled = operation in ("contrast", "reduce")
        self.operation_value.setEnabled(enabled)
        if operation != getattr(self, "_last_operation", None):
            if operation == "contrast":
                self.operation_value.setRange(-100, 100)
                self.operation_value.setPrefix("Contraste ")
                self.operation_value.setValue(0)
            elif operation == "reduce":
                self.operation_value.setRange(1, 12000)
                self.operation_value.setPrefix("Dimension max. ")
                self.operation_value.setValue(1200)
        self._last_operation = operation

    def apply_processing(self, *_args):
        if self._source is None:
            return
        self._render_generation += 1
        generation = self._render_generation
        operation = self.operation_combo.currentData()
        self.apply_button.setEnabled(False)
        value = self.operation_value.value()
        if operation == "reduce" and value == 0:
            value = 1200
            self.operation_value.setValue(value)
        task = ImageTask(self._source, self._source_raw, operation, value)
        task.signals.success.connect(lambda image, generation=generation:
                                     self._processing_success(image, generation))
        task.signals.failure.connect(lambda message, generation=generation:
                                     self._processing_failure(message, generation))
        task.signals.finished.connect(lambda generation=generation:
                                      self.apply_button.setEnabled(True)
                                      if generation == self._render_generation else None)
        self._start_task(task)

    def _processing_success(self, image: np.ndarray, generation: int):
        if generation != self._render_generation:
            return
        self._result = image
        self._show_image(self.result_label, image)
        self.save_result_button.setEnabled(True)

    def _processing_failure(self, message: str, generation: int):
        if generation != self._render_generation and generation != self._source_generation:
            return
        self.result_label.setText(f"Traitement impossible : {message}")
        self.save_result_button.setEnabled(False)
        self.processing_status.setText(f"Échec du traitement : {message}")

    def save_result(self):
        if self._result is None or self._source_path is None:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Enregistrer une nouvelle image", str(self._source_path.with_name(self._source_path.stem + "-traitee.png")),
            "PNG (*.png);;JPEG (*.jpg *.jpeg)")
        if not filename:
            return
        self.save_result_button.setEnabled(False)
        self._busy_save = True
        self.processing_status.setText("Enregistrement du résultat… la source ne sera pas remplacée.")
        task = SaveTask(self._result, self._source_path, filename)
        task.signals.success.connect(self._save_success)
        task.signals.failure.connect(lambda message: self._error("Enregistrement impossible", message))
        task.signals.failure.connect(lambda message: self.processing_status.setText(
            f"Échec d’enregistrement : {message}"))
        task.signals.finished.connect(self._save_finished)
        self._start_task(task)

    @Slot(str)
    def _save_success(self, filename: str):
        self.source_info.setText(f"Nouvelle image enregistrée : {filename} — l’original est conservé.")
        self.processing_status.setText(f"Nouvelle image enregistrée : {filename}.")

    def _save_finished(self):
        self._busy_save = False
        self.save_result_button.setEnabled(self._result is not None)
        if self._closing:
            self._finish_close()

    @staticmethod
    def _error(title, error):
        QMessageBox.critical(None, title, str(error))

    def closeEvent(self, event: QCloseEvent):
        if self._busy_capture or self._busy_save:
            activity = "capture caméra" if self._busy_capture else "enregistrement du résultat"
            answer = QMessageBox.question(
                self, "Opération en cours",
                f"La fermeture attend la fin de l’opération ({activity}).",
                QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Close:
                event.ignore()
                return
        if self.tabs.currentIndex() == 0 and self._dirty() and not self._resolve_dirty():
            event.ignore()
            return
        self._closing = True
        self.preview_token = None
        self.stop_preview()
        if self._busy_capture or self._busy_save or (self.preview_worker and self.preview_worker.isRunning()):
            event.ignore()
            return
        event.accept()

    def _finish_close(self):
        if self.preview_worker and self.preview_worker.isRunning():
            return
        if self._busy_capture or self._busy_save:
            return
        self.close()


def main() -> int:
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
