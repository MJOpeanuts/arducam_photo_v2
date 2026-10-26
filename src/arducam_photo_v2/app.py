from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFont, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QStackedWidget, QStyle, QStyleOptionSlider, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import resources
from .banks import BankStore
from .camera import (GATE, MAX_STABILIZATION_ATTEMPTS, CameraError, CameraReadError,
                     CameraSelectionError, _cv2, apply_focus, list_devices, open_device,
                     release_device, resolve_device, validate_color)
from .config import DEFAULT_BACKEND, FOCUS_MAX, FOCUS_MIN, MODES, Bank, CameraDevice
from .paths import default_photo_dir
from .processing import archive_capture, load_source, process_color, raw_demosaic, save_processed

ORANGE = "#FF6F4C"
ORANGE_DARK = "#D9512F"
INK = "#20252B"
STAGE = "#222A31"

APP_STYLE = f"""
QWidget {{ background: #F6F5F3; color: {INK}; font-family: 'Segoe UI'; font-size: 15px; }}
QTabWidget::pane {{ border: 0; }}
QTabWidget::tab-bar {{ left: 0; }}
QTabBar {{ border: 0; }}
QTabBar::tab {{ min-height: 46px; min-width: 120px; padding: 0 18px; margin-right: 4px; border: 0;
  border-bottom: 4px solid transparent; color: #66717C; font-size: 17px; font-weight: 600; }}
QTabBar::tab:selected {{ color: {INK}; border-bottom-color: {ORANGE}; background: white; }}
QPushButton, QComboBox, QLineEdit, QSpinBox {{ min-height: 48px; padding: 0 12px;
  border: 1px solid #D5D9DE; border-radius: 9px; background: white; }}
QPushButton {{ font-weight: 600; }}
QPushButton:hover, QComboBox:hover {{ border-color: {ORANGE}; }}
QPushButton:checked {{ border: 2px solid {ORANGE}; }}
QPushButton:disabled, QComboBox:disabled {{ color: #9AA2AA; background: #ECECEA; }}
QPushButton#primary {{ color: white; border: 0; background: {ORANGE_DARK}; font-size: 18px; }}
QPushButton#primary:hover {{ background: #C2452A; }}
QPushButton#primary:disabled {{ color: #F4F4F4; background: #E3B1A3; }}
QPushButton#photo {{ min-height: 68px; font-size: 25px; font-weight: 700; color: white; border: 0;
  background: {ORANGE_DARK}; }}
QPushButton#photo:hover {{ background: #C2452A; }}
QPushButton#photo:disabled {{ color: #F4F4F4; background: #E3B1A3; }}
QComboBox::drop-down {{ border: 0; width: 34px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url("{resources.file_path(resources.CHEVRON)}"); width: 22px; height: 22px; }}
QComboBox QAbstractItemView {{ min-height: 44px; selection-background-color: #FFE1D8; selection-color: {INK}; }}
QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 12px; background: #DCE0E4; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: #FFB4A1; border-radius: 6px; }}
QSlider::handle:horizontal {{ width: 44px; height: 44px; margin: -16px 0; border-radius: 22px; background: {ORANGE}; }}
QSlider::handle:horizontal:disabled {{ background: #C9CED3; }}
QProgressBar {{ min-height: 14px; max-height: 14px; border: 0; border-radius: 7px; background: #E0E4E8; }}
QProgressBar::chunk {{ background: {ORANGE}; border-radius: 7px; }}
QFrame#card {{ background: white; border: 1px solid #E6E3DF; border-radius: 14px; }}
QFrame#card QLabel {{ background: transparent; }}
QLabel#heading {{ font-size: 19px; font-weight: 700; }}
QLabel#muted {{ color: #66717C; font-size: 14px; }}
QLabel#value {{ font-size: 26px; font-weight: 700; color: {INK}; }}
QLabel#fault {{ color: white; background: #B3261E; border-radius: 8px; padding: 8px 12px; font-weight: 600; }}
QTextEdit {{ border: 1px solid #D5D9DE; border-radius: 8px; background: white; font-size: 13px; }}
QScrollArea {{ border: 0; }}
"""

TOUCH_MIN = 48


def _ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app else None
    return screen.devicePixelRatio() if screen else 1.0


def _button(text: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setMinimumHeight(TOUCH_MIN)
    if primary:
        button.setObjectName("primary")
    return button


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _muted(text: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(wrap)
    return label


def to_qimage(image: np.ndarray) -> QImage:
    array = np.ascontiguousarray(image)
    fmt = QImage.Format.Format_Grayscale8 if array.ndim == 2 else QImage.Format.Format_BGR888
    return QImage(array.data, array.shape[1], array.shape[0], array.strides[0], fmt).copy()


class ImageView(QWidget):
    """Working image area: the image fills it; empty states show the squirrel icon."""

    def __init__(self, message: str = "", parent=None):
        super().__init__(parent)
        self._message = message
        self._image: QImage | None = None
        self.setMinimumSize(240, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def setText(self, message: str) -> None:
        self._message = message
        self._image = None
        self.update()

    def text(self) -> str:
        return self._message

    def has_image(self) -> bool:
        return self._image is not None

    def set_qimage(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_array(self, image: np.ndarray) -> None:
        self.set_qimage(to_qimage(image))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.fillPath(path, QColor(STAGE))
        if self._image is not None and not self._image.isNull():
            size = QSize(self._image.width(), self._image.height()).scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                            size.width(), size.height())
            painter.setClipPath(path)
            painter.drawImage(target, self._image)
            return
        icon_size = max(32, min(72, self.height() // 4))
        icon = resources.squirrel(icon_size, ORANGE, self.devicePixelRatioF())
        text_top = self.height() / 2 + 6
        painter.drawPixmap(int((self.width() - icon_size) / 2), int(text_top - icon_size - 10), icon)
        painter.setPen(QColor("#E9ECEF"))
        font = QFont(self.font())
        font.setPixelSize(16)
        painter.setFont(font)
        painter.drawText(QRectF(24, text_top, self.width() - 48, self.height() - text_top - 12),
                         int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap),
                         self._message)


class TouchSlider(QSlider):
    """Horizontal slider whose whole widget height is the touch zone.

    A press anywhere on the track moves the handle there and starts dragging,
    so the command is still sent only on release.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(hint.width(), max(hint.height(), 56))

    def handle_rect(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        return self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                           QStyle.SubControl.SC_SliderHandle, self)

    def _value_at(self, x: float) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                             QStyle.SubControl.SC_SliderGroove, self)
        handle = self.handle_rect()
        span = max(1, groove.width() - handle.width())
        position = int(x - groove.x() - handle.width() / 2)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                              max(0, min(span, position)), span)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setSliderDown(True)
        self.setSliderPosition(self._value_at(event.position().x()))
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.isSliderDown():
            return super().mouseMoveEvent(event)
        self.setSliderPosition(self._value_at(event.position().x()))
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isSliderDown():
            return super().mouseReleaseEvent(event)
        self.setSliderDown(False)
        event.accept()


class TouchKeyboard(QDialog):
    def __init__(self, title: str, initial: str = "", numeric: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        self.entry = QLineEdit(initial)
        self.entry.setMinimumHeight(56)
        layout.addWidget(self.entry)
        grid = QGridLayout()
        grid.setSpacing(6)
        rows = ["123", "456", "789", "0"] if numeric else ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm-"]
        for row_number, row in enumerate(rows):
            for column, character in enumerate(row):
                key = _button(character.upper() if not numeric else character)
                key.setMinimumSize(TOUCH_MIN if not numeric else 72, TOUCH_MIN)
                key.clicked.connect(lambda checked=False, char=character: self.entry.insert(char))
                grid.addWidget(key, row_number, column + (1 if numeric and row == "0" else 0))
        layout.addLayout(grid)
        controls = QHBoxLayout()
        backspace = _button("⌫ Effacer")
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
    """One Preview session on one device. Its token tags every event it emits."""

    frame = Signal(str, object)
    status = Signal(str, str)
    focus_result = Signal(str, int, int, object)

    def __init__(self, session_id: str, device: CameraDevice, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.device = device
        self._stop = threading.Event()
        self._focus_queue: queue.SimpleQueue[tuple[int, int]] = queue.SimpleQueue()
        self.release_error: str | None = None
        self.terminal_error: str | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self):
        self._stop.set()

    def set_focus(self, sequence: int, value: int):
        self._focus_queue.put((sequence, value))

    def take_unapplied(self) -> tuple[int, int] | None:
        latest = None
        while not self._focus_queue.empty():
            latest = self._focus_queue.get()
        return latest

    def _apply_pending(self, cap, cv2_module):
        latest = self.take_unapplied()
        if latest is None or self._stop.is_set():
            if latest is not None:
                self._focus_queue.put(latest)
            return
        sequence, value = latest
        self.focus_result.emit(self.session_id, sequence, value, apply_focus(cap, value, cv2_module))

    def run(self):
        if self._stop.is_set():
            return
        try:
            GATE.claim("preview")
        except CameraError as exc:
            self.terminal_error = f"Aperçu impossible : {exc}. Utilisez « Relancer le Preview »."
            self.status.emit(self.session_id, self.terminal_error)
            return
        cap = None
        try:
            self.status.emit(self.session_id, f"Ouverture de {self.device.label}…")
            cv2_module = _cv2()
            self.device = resolve_device(self.device, list_devices(DEFAULT_BACKEND))
            self.status.emit(
                self.session_id, f"Backend choisi : {self.device.backend} ; disponibilité : oui. "
                f"Caméra résolue : {self.device.label} ; chemin : {self.device.path or 'indisponible'}.")
            if self._stop.is_set():
                return
            cap = open_device(self.device)
            if cap is None or not cap.isOpened():
                raise CameraError("Étape ouverture : caméra inaccessible.")
            for prop, value, label in ((cv2_module.CAP_PROP_FRAME_WIDTH, 1280, "largeur"),
                                       (cv2_module.CAP_PROP_FRAME_HEIGHT, 720, "hauteur")):
                if not cap.set(prop, value):
                    raise CameraError(f"Étape configuration : le pilote a refusé la {label} 720p.")
            self.status.emit(self.session_id, "Aperçu de cadrage — en attente d’image.")
            failures = 0
            last_shape = None
            dark_state = None
            while not self._stop.is_set():
                self._apply_pending(cap, cv2_module)
                try:
                    ok, frame = cap.read()
                except Exception as exc:
                    raise CameraReadError(f"Étape lecture caméra : {exc}") from exc
                try:
                    if not ok:
                        raise CameraReadError(
                            f"Lecture échouée : ok={ok}, forme={getattr(frame, 'shape', None)}, "
                            f"type={getattr(frame, 'dtype', None)}.")
                    frame = validate_color(frame, "color_720p")
                except CameraReadError as exc:
                    failures += 1
                    if failures >= MAX_STABILIZATION_ATTEMPTS:
                        raise CameraReadError(
                            f"Étape lecture 720p : {failures} lectures consécutives non conformes. {exc}"
                        ) from exc
                    self.status.emit(self.session_id, f"Image d’aperçu transitoire ignorée : {exc}")
                    self.msleep(60)
                    continue
                failures = 0
                if frame.shape != last_shape:
                    self.status.emit(self.session_id,
                                     f"Dimensions réellement reçues : {frame.shape}, {frame.dtype} (BGR).")
                    last_shape = frame.shape
                is_dark = float(np.mean(frame[::16, ::16])) < 8
                if is_dark != dark_state:
                    self.status.emit(self.session_id,
                                     "Image reçue mais sombre — aucun réglage photométrique automatique."
                                     if is_dark else "Aperçu actif — vérifiez qu’il s’agit de la bonne caméra.")
                    dark_state = is_dark
                self.frame.emit(self.session_id, to_qimage(frame))
                self.msleep(15)
        except Exception as exc:
            self.terminal_error = f"Erreur d’aperçu : {exc} Utilisez « Relancer le Preview »."
        finally:
            if cap is not None:
                self.release_error = release_device(cap)
            GATE.release(self.release_error)
            self.status.emit(self.session_id, self.release_error or self.terminal_error or "Aperçu libéré.")


class TaskSignals(QObject):
    progress = Signal(str)
    success = Signal(object)
    failure = Signal(str)
    finished = Signal()


class CaptureTask(QRunnable):
    def __init__(self, bank: Bank, photo_dir: Path, device: CameraDevice | None = None):
        super().__init__()
        self.bank = bank
        self.photo_dir = photo_dir
        self.device = device
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            from .camera import acquire
            identifier = str(uuid.uuid4())
            self.signals.progress.emit("Ouverture caméra, configuration et stabilisation du mode…")
            result = acquire(self.bank, identifier, device=self.device)
            self.signals.progress.emit(
                f"Backend choisi : {result.device.backend} ; disponibilité : oui. "
                f"Caméra résolue : {result.device.label} ; chemin : {result.device.path}. "
                f"Dimensions réellement reçues : {result.received_shape}, {result.dtype}.")
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
        self.setWindowIcon(resources.window_icon())
        self.setMinimumSize(800, 440)
        self.store = store or BankStore()
        self.photo_dir = default_photo_dir()
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(1)
        self.devices: list[CameraDevice] = []
        self.session_device: CameraDevice | None = None
        self.work_bank = self._empty_draft()
        self.saved_bank: Bank | None = None
        self._initial_empty_draft = True
        self._capture_bank_id: str | None = None
        self.preview_worker: PreviewWorker | None = None
        self.preview_token: str | None = None
        self._restart_requested = False
        self._preview_failed = False
        self._pending_focus: tuple[int, int] | None = None
        self._focus_seq = 0
        self._focus_report: dict = {"state": "none"}
        self.previous_tab = 0
        self._changing_tab = False
        self._busy_capture = False
        self._busy_save = False
        self._task: QRunnable | None = None
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
        self._build()
        self.reload_banks()
        self.refresh_work_controls()
        if self.store.reset_notice:
            self.notice.setText(self.store.reset_notice)
        QTimer.singleShot(0, self.refresh_devices)

    # ---------------------------------------------------------------- layout
    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 6, 12, 4)
        outer.setSpacing(6)
        self.fault_banner = QLabel()
        self.fault_banner.setObjectName("fault")
        self.fault_banner.setWordWrap(True)
        self.fault_banner.hide()
        outer.addWidget(self.fault_banner)
        self.tabs = QTabWidget()
        self.logo = QLabel()
        self.logo.setObjectName("logo")
        self.logo.setPixmap(resources.pixmap(resources.LOGO, 40, _ratio()))
        self.logo.setContentsMargins(2, 2, 18, 4)
        self.logo.setAccessibleName("Data Peanuts")
        self.tabs.setCornerWidget(self.logo, Qt.Corner.TopLeftCorner)
        self.preview_page = self._build_preview()
        self.capture_page = self._build_capture()
        self.processing_page = self._build_processing()
        self.tabs.addTab(self.preview_page, "Preview")
        self.tabs.addTab(self.capture_page, "Capture")
        self.tabs.addTab(self.processing_page, "Traitement")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 0, 0)
        self.notice = _muted("", wrap=False)
        self.notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.notice, 1)
        self.powered_by = QLabel()
        self.powered_by.setObjectName("poweredBy")
        self.powered_by.setPixmap(resources.pixmap(resources.POWERED_BY, 18, _ratio()))
        self.powered_by.setAccessibleName("powered by Data Peanuts")
        footer.addWidget(self.powered_by)
        outer.addLayout(footer)
        self.setCentralWidget(root)

    def _build_preview(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        stage = QVBoxLayout()
        stage.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.camera_combo = QComboBox()
        self.camera_combo.setAccessibleName("Caméra")
        self.camera_combo.setToolTip("Caméra utilisée par le Preview et enregistrée dans la banque")
        self.camera_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.camera_combo.setMinimumContentsLength(10)
        self.camera_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.camera_combo.activated.connect(self._camera_chosen)
        self.refresh_cameras_button = _button("Actualiser")
        self.refresh_cameras_button.setToolTip("Actualiser la liste des caméras")
        self.refresh_cameras_button.clicked.connect(self.refresh_devices)
        self.work_selector = QComboBox()
        self.work_selector.setAccessibleName("Banque")
        self.work_selector.setToolTip("Charger une banque sauvegardée (applique son Focus manuel)")
        self.work_selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.work_selector.setMinimumContentsLength(8)
        self.work_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.work_selector.activated.connect(self._work_selected)
        toolbar.addWidget(QLabel("Caméra"))
        toolbar.addWidget(self.camera_combo, 3)
        toolbar.addWidget(self.refresh_cameras_button)
        toolbar.addWidget(QLabel("Banque"))
        toolbar.addWidget(self.work_selector, 3)
        stage.addLayout(toolbar)
        self.preview_image = ImageView("Recherche des caméras…")
        stage.addWidget(self.preview_image, 1)
        self.preview_status = _muted("Aperçu de cadrage inactif.", wrap=False)
        self.preview_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        stage.addWidget(self.preview_status)
        self.preview_disclaimer = _muted(
            "Aperçu de cadrage 720p : le mode et le champ de vue de la photo peuvent différer.",
            wrap=False)
        self.preview_disclaimer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        stage.addWidget(self.preview_disclaimer)
        layout.addLayout(stage, 1)

        self.preview_panel = QStackedWidget()
        self.preview_panel.setFixedWidth(340)
        self.preview_panel.addWidget(self._build_preview_controls())
        self.preview_panel.addWidget(self._build_preview_details())
        layout.addWidget(self.preview_panel)
        return page

    def _build_preview_controls(self):
        card = _card()
        form = QVBoxLayout(card)
        form.setContentsMargins(14, 10, 14, 12)
        form.setSpacing(7)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.work_title = QLabel()
        self.work_title.setObjectName("heading")
        self.work_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.dirty_label = _muted("", wrap=False)
        titles.addWidget(self.work_title)
        titles.addWidget(self.dirty_label)
        head.addLayout(titles, 1)
        self.preview_details_toggle = _button("Détails")
        self.preview_details_toggle.setToolTip("Explications, identité caméra et diagnostic")
        self.preview_details_toggle.clicked.connect(lambda: self.preview_panel.setCurrentIndex(1))
        head.addWidget(self.preview_details_toggle)
        form.addLayout(head)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode d’acquisition"))
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName("Mode d’acquisition")
        self.mode_combo.setToolTip("Mode d’acquisition de la future capture")
        for key, descriptor in MODES.items():
            self.mode_combo.addItem(descriptor.label, key)
        self.mode_combo.activated.connect(self._mode_changed)
        mode_row.addWidget(self.mode_combo, 1)
        form.addLayout(mode_row)
        focus_row = QHBoxLayout()
        focus_row.setSpacing(6)
        self.focus_minus = _button("−")
        self.focus_plus = _button("+")
        for button, label in ((self.focus_minus, "Focus manuel −1"), (self.focus_plus, "Focus manuel +1")):
            button.setFixedWidth(56)
            button.setAutoRepeat(False)
            button.setToolTip(label)
            button.setStyleSheet("font-size: 24px;")
        self.focus_value = QLabel("—")
        self.focus_value.setObjectName("value")
        self.focus_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.focus_value.setMinimumWidth(70)
        self.focus_input = _button("Saisir")
        self.focus_input.setAccessibleName("Saisir le Focus manuel")
        self.focus_input.setToolTip("Saisir le Focus manuel (entier de 0 à 1023)")
        self.focus_minus.clicked.connect(lambda: self.adjust_focus(-1))
        self.focus_plus.clicked.connect(lambda: self.adjust_focus(1))
        self.focus_input.clicked.connect(self.enter_focus)
        focus_caption = QLabel("Focus manuel")
        focus_caption.setStyleSheet("font-weight:600")
        focus_row.addWidget(focus_caption)
        focus_row.addWidget(self.focus_minus)
        focus_row.addWidget(self.focus_value, 1)
        focus_row.addWidget(self.focus_plus)
        focus_row.addWidget(self.focus_input)
        form.addLayout(focus_row)
        self.focus_slider = TouchSlider()
        self.focus_slider.setAccessibleName("Focus manuel")
        self.focus_slider.setRange(FOCUS_MIN, FOCUS_MAX)
        self.focus_slider.setSingleStep(1)
        self.focus_slider.setPageStep(16)
        self.focus_slider.sliderMoved.connect(self._focus_moving)
        self.focus_slider.sliderReleased.connect(self._focus_released)
        form.addWidget(self.focus_slider)
        status_row = QHBoxLayout()
        self.focus_command_status = _muted()
        self.focus_command_status.setMinimumHeight(40)
        self.focus_resend = _button("Renvoyer")
        self.focus_resend.setToolTip("Renvoyer la même consigne de Focus manuel au Preview")
        self.focus_resend.clicked.connect(self.resend_focus)
        status_row.addWidget(self.focus_command_status, 1)
        status_row.addWidget(self.focus_resend)
        form.addLayout(status_row)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.save_bank_button = _button("Sauvegarder la banque", primary=True)
        self.save_bank_button.clicked.connect(self.save_work_bank)
        self.new_bank_button = _button("Nouvelle")
        self.new_bank_button.setToolTip("Créer une nouvelle banque")
        self.new_bank_button.clicked.connect(self.create_draft)
        self.work_name_button = _button("Nommer")
        self.work_name_button.setToolTip("Renommer la banque de travail")
        self.work_name_button.clicked.connect(self.rename_work_bank)
        actions.addWidget(self.save_bank_button, 2)
        actions.addWidget(self.new_bank_button, 1)
        actions.addWidget(self.work_name_button, 1)
        form.addLayout(actions)
        form.addStretch(1)
        return card

    def _build_preview_details(self):
        card = _card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 10, 14, 12)
        back = _button("← Réglages")
        back.clicked.connect(lambda: self.preview_panel.setCurrentIndex(0))
        self.preview_details_back = back
        outer.addWidget(back)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 4, 0)
        for text in (
            "Aperçu de cadrage en 720p uniquement ; la banque choisit le Mode d’acquisition photo. "
            "Champ de vue et netteté "
            "peuvent varier selon le mode.",
            "Focus manuel : entier de 0 à 1023, sans unité ni distance. Le curseur envoie au "
            "relâchement, la saisie à la validation. Une commande acceptée ne signifie pas une mise "
            "au point réussie : vérifiez la netteté sur l’image.",
            "Caméra : l’index OpenCV n’est pas un identifiant stable. La banque enregistre le chemin du "
            "périphérique et son nom ; si la caméra attendue est absente ou ambiguë, une sélection est demandée.",
        ):
            column.addWidget(_muted(text))
        self.camera_identity = _muted()
        self.camera_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.camera_identity)
        self.restart_preview_button = _button("Relancer le Preview")
        self.restart_preview_button.clicked.connect(self.restart_preview)
        column.addWidget(self.restart_preview_button)
        self.preview_details = QTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setMinimumHeight(120)
        self.preview_details.setPlaceholderText("Les rapports caméra apparaîtront ici.")
        self.preview_details.document().setMaximumBlockCount(200)
        column.addWidget(self.preview_details, 1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        return card

    def _build_capture(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        card = _card()
        card.setFixedWidth(340)
        left = QVBoxLayout(card)
        left.setContentsMargins(14, 10, 14, 12)
        left.setSpacing(8)
        self.capture_selector = QComboBox()
        capture_heading = QLabel("Banque utilisée pour la capture")
        capture_heading.setObjectName("heading")
        left.addWidget(capture_heading)
        self.capture_selector.setAccessibleName("Banque utilisée pour la capture")
        self.capture_selector.setToolTip("Sélectionner une banque sauvegardée pour PHOTO")
        self.capture_selector.activated.connect(self._capture_bank_changed)
        left.addWidget(self.capture_selector)
        self.capture_summary = QLabel("Sélectionnez une banque complète.")
        self.capture_summary.setWordWrap(True)
        self.capture_summary.setTextFormat(Qt.TextFormat.RichText)
        left.addWidget(self.capture_summary)
        self.photo_button = _button("PHOTO", primary=True)
        self.photo_button.setObjectName("photo")
        self.photo_button.clicked.connect(self.take_photo)
        left.addWidget(self.photo_button)
        self.capture_progress = QProgressBar()
        self.capture_progress.setRange(0, 0)
        self.capture_progress.setTextVisible(False)
        self.capture_progress.hide()
        left.addWidget(self.capture_progress)
        self.capture_reason = _muted()
        left.addWidget(self.capture_reason)
        self.capture_state = _muted("Aucune opération en cours.")
        self.capture_state.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.capture_state)
        left.addStretch(1)
        self.photo_path_label = _muted(str(self.photo_dir))
        left.addWidget(self.photo_path_label)
        folder_row = QHBoxLayout()
        self.choose_folder_button = _button("Dossier…")
        self.choose_folder_button.clicked.connect(self.choose_photo_dir)
        self.open_folder_button = _button("Ouvrir")
        self.open_folder_button.setToolTip("Ouvrir le dossier des photos")
        self.open_folder_button.clicked.connect(self.open_photo_folder)
        self.open_last_button = _button("Dernière")
        self.open_last_button.setToolTip("Ouvrir la dernière image")
        self.open_last_button.clicked.connect(self.open_last_image)
        self.open_last_button.setEnabled(False)
        folder_row.addWidget(self.choose_folder_button)
        folder_row.addWidget(self.open_folder_button)
        folder_row.addWidget(self.open_last_button)
        left.addLayout(folder_row)
        self.preview_link = _button("Préparer une banque dans Preview")
        self.preview_link.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        left.addWidget(self.preview_link)
        layout.addWidget(card)
        self.capture_image = ImageView("Aucune image capturée")
        layout.addWidget(self.capture_image, 1)
        return page

    def _build_processing(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        controls = QHBoxLayout()
        self.open_source_button = _button("Ouvrir acquisition / image…")
        self.open_source_button.clicked.connect(self.open_source)
        controls.addWidget(self.open_source_button)
        self.operation_combo = QComboBox()
        self.operation_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.operation_combo.setMinimumContentsLength(10)
        self.operation_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.operation_combo.currentIndexChanged.connect(self._operation_changed)
        controls.addWidget(self.operation_combo, 1)
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
        self.source_info = _muted("Ouvrez un manifeste d’acquisition JSON, une image PNG ou JPEG.", wrap=False)
        self.source_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.source_info)
        self.processing_status = _muted("Aucun traitement en cours. Aucun accès caméra depuis cet onglet.", wrap=False)
        self.processing_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.processing_status)
        images = QHBoxLayout()
        source_box = QVBoxLayout()
        source_box.addWidget(_muted("SOURCE", wrap=False))
        self.source_label = ImageView("Aucune source ouverte")
        source_box.addWidget(self.source_label, 1)
        result_box = QVBoxLayout()
        self.result_heading = _muted("RÉSULTAT", wrap=False)
        result_box.addWidget(self.result_heading)
        self.result_label = ImageView("Choisissez un traitement compatible.")
        result_box.addWidget(self.result_label, 1)
        images.addLayout(source_box, 1)
        images.addLayout(result_box, 1)
        layout.addLayout(images, 1)
        return page

    def _show_image(self, view: ImageView, image: np.ndarray):
        view.set_array(image)

    # --------------------------------------------------------------- cameras
    def refresh_devices(self):
        """Explicit enumeration; never opens a device and never picks one silently."""
        if GATE.fault:
            self._enter_fault()
            return
        try:
            self.devices = list_devices(DEFAULT_BACKEND)
            self.preview_details.append(f"Backend choisi : {DEFAULT_BACKEND} ; disponibilité : oui.")
            error = None
        except Exception as exc:
            self.devices = []
            error = f"Énumération des caméras impossible : {exc}"
            self.preview_details.append(error)
        expected = self.session_device or self.work_bank.camera
        resolved = None
        message = error
        if expected is not None and error is None:
            try:
                resolved = resolve_device(expected, self.devices)
            except CameraSelectionError as exc:
                message = str(exc)
        self.session_device = resolved
        self._populate_cameras()
        if message is None and resolved is None:
            message = ("Choisissez la caméra dans la liste ; l’aperçu permet de vérifier qu’il s’agit de la bonne."
                       if self.devices else "Aucune caméra détectée. Branchez-la puis touchez Actualiser.")
        if message:
            self._set_preview_message(message)
        self._ensure_preview()

    def _populate_cameras(self):
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self.camera_combo.addItem("Choisir une caméra…" if self.devices else "Aucune caméra détectée", None)
        for device in self.devices:
            self.camera_combo.addItem(device.label, device)
            self.camera_combo.setItemData(self.camera_combo.count() - 1, device.path or "", Qt.ItemDataRole.ToolTipRole)
        index = 0
        if self.session_device is not None:
            for position in range(1, self.camera_combo.count()):
                if self.camera_combo.itemData(position) == self.session_device:
                    index = position
        self.camera_combo.setCurrentIndex(index)
        self.camera_combo.blockSignals(False)
        self._update_camera_identity()

    def _update_camera_identity(self):
        device = self.session_device
        self.camera_identity.setText(
            f"Backend choisi : {DEFAULT_BACKEND}\nCaméra du Preview : aucune" if device is None else
            f"Caméra du Preview : {device.name or 'nom indisponible'}\nBackend {device.backend}, "
            f"index actuel {device.index}\nChemin : {device.path or 'indisponible'}")

    def _camera_chosen(self, index: int):
        device = self.camera_combo.itemData(index)
        if device is None:
            return
        self.session_device = device
        self.work_bank = replace(self.work_bank, camera=device)
        self._update_dirty()
        self._update_camera_identity()
        self._set_preview_message(f"Ouverture de {device.label} — vérifiez l’image.")
        self._ensure_preview()

    def _set_preview_message(self, message: str):
        self.preview_status.setText(message)
        self.preview_status.setToolTip(message)
        if self.preview_worker is None or not self.preview_image.has_image():
            self.preview_image.setText(message)

    # --------------------------------------------------------------- preview
    def _preview_wanted(self) -> bool:
        return (self.tabs.currentIndex() == 0 and self.session_device is not None and not GATE.fault
                and not self._preview_failed and not self._busy_capture and not self._closing)

    def _session_active(self) -> bool:
        worker = self.preview_worker
        return worker is not None and not worker.stopping and worker.session_id == self.preview_token

    def _ensure_preview(self):
        """Converge on at most one session, on the selected device. A session is
        always fully released before the next one opens."""
        worker = self.preview_worker
        if worker is not None:
            same_device = (self.session_device is not None
                           and worker.device.identity == self.session_device.identity)
            if not self._preview_wanted() or self._restart_requested or not same_device:
                if not worker.stopping:
                    worker.stop()
                    self.preview_token = None
                    self.preview_image.setText("Libération de la caméra…")
            return
        if self._preview_wanted():
            self._start_preview_worker()

    def start_preview(self):
        self._ensure_preview()

    def _start_preview_worker(self):
        self._restart_requested = False
        token = str(uuid.uuid4())
        worker = PreviewWorker(token, self.session_device)
        if self._pending_focus is not None:
            sequence, value = self._pending_focus
            self._pending_focus = None
            worker.set_focus(sequence, value)
            self._focus_report = {"seq": sequence, "value": value, "state": "sent"}
        elif self.work_bank.focus is not None:
            self._focus_report = {"value": self.work_bank.focus, "state": "not_applied"}
        else:
            self._focus_report = {"state": "none"}
        worker.frame.connect(self._preview_frame)
        worker.status.connect(self._preview_status)
        worker.focus_result.connect(self._focus_result)
        worker.finished.connect(lambda worker=worker: self._preview_finished(worker))
        self.preview_worker = worker
        self.preview_token = token
        self.preview_image.setText(f"Ouverture de {self.session_device.label}…")
        self._render_focus_status()
        worker.start()

    def stop_preview(self):
        if self.preview_worker is not None:
            self.preview_worker.stop()
            self.preview_token = None

    def restart_preview(self):
        self._preview_failed = False
        self._restart_requested = True
        self.preview_status.setText("Libération puis réouverture de l’aperçu…")
        self._ensure_preview()

    @Slot(str, object)
    def _preview_frame(self, token: str, frame):
        if token != self.preview_token or self.tabs.currentIndex() != 0:
            return
        if isinstance(frame, np.ndarray):
            self.preview_image.set_array(frame)
        else:
            self.preview_image.set_qimage(frame)

    @Slot(str, str)
    def _preview_status(self, token: str, message: str):
        self.preview_details.append(message)
        if token == self.preview_token:
            self.preview_status.setText(message)
            self.preview_status.setToolTip(message)
            if not self.preview_image.has_image():
                self.preview_image.setText(message)

    @Slot(str, int, int, object)
    def _focus_result(self, token: str, sequence: int, value: int, report: dict):
        self.preview_details.append(json.dumps({"session": token[:8], **report}, ensure_ascii=False))
        if token != self.preview_token or sequence != self._focus_seq:
            return
        accepted = report.get("accepted")
        state = "accepted" if accepted is True else "refused" if accepted is False else "error"
        self._focus_report = {"seq": sequence, "value": value, "state": state, "report": report}
        self._render_focus_status()

    def _preview_finished(self, worker: PreviewWorker):
        leftover = worker.take_unapplied()
        if leftover is not None and leftover[0] == self._focus_seq:
            self._focus_report = {"seq": leftover[0], "value": leftover[1], "state": "dropped"}
            self._render_focus_status()
        if worker is not self.preview_worker:
            return
        self.preview_worker = None
        self.preview_token = None
        if worker.terminal_error and not self._restart_requested:
            self._preview_failed = True
            self._set_preview_message(worker.terminal_error)
        if GATE.fault:
            self._enter_fault()
        if self.tabs.currentIndex() == 1:
            self._update_capture_bank()
        if self._closing:
            self._finish_close()
            return
        if self.tabs.currentIndex() == 0 and self.session_device is None:
            self.preview_image.setText("Choisissez une caméra pour démarrer le Preview.")
        self._ensure_preview()

    # ----------------------------------------------------------------- focus
    def _focus_sync(self):
        """Graphic synchronisation only: never sends a camera command."""
        focus = self.work_bank.focus
        self.focus_slider.blockSignals(True)
        self.focus_slider.setValue(focus if focus is not None else FOCUS_MIN)
        self.focus_slider.blockSignals(False)
        self.focus_value.setText(str(focus) if focus is not None else "—")
        self.focus_value.setToolTip(
            "Focus manuel non défini" if focus is None else f"Focus manuel : consigne {focus} / 1023")
        self.focus_minus.setEnabled(focus is not None and focus > FOCUS_MIN)
        self.focus_plus.setEnabled(focus is not None and focus < FOCUS_MAX)
        self._render_focus_status()

    def _render_focus_status(self):
        report = self._focus_report
        state = report.get("state", "none")
        value = report.get("value")
        detail = report.get("report", {})
        if state == "none" or value is None:
            text = ("Focus manuel non défini : touchez le curseur, ± ou Saisir."
                    if self.work_bank.focus is None else "Aucune consigne envoyée.")
        elif state == "sent":
            text = f"Consigne {value} envoyée — en attente du pilote."
        elif state == "deferred":
            text = f"Consigne {value} — appliquée à l’ouverture du Preview."
        elif state == "accepted":
            readback = detail.get("readback")
            reading = "relecture indisponible" if readback is None else f"relecture {readback:g}"
            text = f"Consigne {value} · acceptée · {reading}. Netteté à vérifier sur l’image."
        elif state == "refused":
            text = f"Consigne {value} · refusée par le pilote."
        elif state == "error":
            text = f"Consigne {value} · erreur : {detail.get('error', 'inconnue')}"
        elif state == "dropped":
            text = f"Consigne {value} non appliquée : session fermée."
        else:
            text = f"Consigne {value} non envoyée à cette session."
        self.focus_command_status.setText(text)
        self.focus_command_status.setToolTip(text)
        self.focus_resend.setEnabled(self.work_bank.focus is not None and state not in ("sent", "deferred")
                                     and not GATE.fault)

    def _send_focus(self, value: int):
        """Explicit command; the latest one supersedes any queued command."""
        self._focus_seq += 1
        sequence = self._focus_seq
        if self._session_active():
            self._pending_focus = None
            self.preview_worker.set_focus(sequence, value)
            state = "sent"
        else:
            self._pending_focus = (sequence, value)
            state = "deferred"
        self._focus_report = {"seq": sequence, "value": value, "state": state}
        self._render_focus_status()

    def _commit_focus(self, value: int):
        value = max(FOCUS_MIN, min(FOCUS_MAX, int(value)))
        self.work_bank = replace(self.work_bank, focus=value)
        self._initial_empty_draft = False
        self._focus_sync()
        self._update_dirty()
        self._send_focus(value)

    def _focus_moving(self, value: int):
        self.focus_value.setText(str(value))

    def _focus_released(self):
        self._commit_focus(self.focus_slider.value())

    def adjust_focus(self, amount: int):
        if self.work_bank.focus is not None:
            self._commit_focus(self.work_bank.focus + amount)

    def resend_focus(self):
        if self.work_bank.focus is not None:
            self._send_focus(self.work_bank.focus)

    def enter_focus(self):
        keyboard = TouchKeyboard("Focus manuel (0–1023)",
                                 "" if self.work_bank.focus is None else str(self.work_bank.focus),
                                 numeric=True, parent=self)
        if keyboard.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            value = int(keyboard.value())
            if not FOCUS_MIN <= value <= FOCUS_MAX:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Focus invalide", "Saisissez un entier compris entre 0 et 1023.")
            return
        self._commit_focus(value)

    # ----------------------------------------------------------------- banks
    @staticmethod
    def _empty_draft() -> Bank:
        return Bank.draft("Nouvelle banque", "color_720p", None)

    @staticmethod
    def _bank_item(bank: Bank) -> str:
        descriptor = MODES[bank.mode]
        focus = "—" if bank.focus is None else bank.focus
        return (f"{bank.name}  ·  Mode d’acquisition : {descriptor.width}×{descriptor.height}"
                f"  ·  Focus manuel : {focus}")

    def reload_banks(self):
        """Rebuild both lists while preserving each current selection."""
        banks = self.store.list()
        known = {bank.id for bank in banks}
        if self._capture_bank_id not in known:
            self._capture_bank_id = None
        wanted = ((self.work_selector, self.saved_bank.id if self.saved_bank else None, "Charger une banque…"),
                  (self.capture_selector, self._capture_bank_id, "Choisir une banque…"))
        for combo, selected, placeholder in wanted:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(placeholder, None)
            for bank in banks:
                combo.addItem(self._bank_item(bank), bank.id)
            combo.setCurrentIndex(max(0, combo.findData(selected)) if selected else 0)
            combo.blockSignals(False)

    def refresh_work_controls(self):
        index = self.mode_combo.findData(self.work_bank.mode)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, index))
        self.mode_combo.blockSignals(False)
        self._focus_sync()
        self._update_dirty()

    def _work_selected(self, index: int):
        """Explicit bank load: syncs the controls, then applies its manual focus."""
        bank_id = self.work_selector.itemData(index)
        if not bank_id:
            self.reload_banks()
            return
        if not self._resolve_dirty():
            self.reload_banks()
            return
        try:
            bank = self.store.get(bank_id)
        except Exception as exc:
            self._error("Chargement de la banque", exc)
            self.reload_banks()
            return
        self.saved_bank = self.work_bank = bank
        self._initial_empty_draft = False
        self._capture_bank_id = bank.id
        self.reload_banks()
        self.refresh_work_controls()
        if bank.camera is not None:
            try:
                self.devices = list_devices(DEFAULT_BACKEND)
                self.session_device = resolve_device(bank.camera, self.devices)
                self._set_preview_message(f"Caméra de la banque : {self.session_device.label}.")
            except Exception as exc:
                self.session_device = None
                self._set_preview_message(f"{exc} Choisissez une caméra.")
            self._populate_cameras()
        self._ensure_preview()
        if bank.focus is not None:
            self._send_focus(bank.focus)

    def create_draft(self):
        if not self._resolve_dirty():
            return
        keyboard = TouchKeyboard("Nom de la banque", parent=self)
        if keyboard.exec() != QDialog.DialogCode.Accepted:
            return
        name = keyboard.value().strip()
        if not name:
            QMessageBox.warning(self, "Nom manquant", "Saisissez un nom de banque.")
            return
        self.saved_bank = None
        self._initial_empty_draft = False
        self.work_bank = Bank.draft(name, self.work_bank.mode, None, self.session_device)
        self._focus_report = {"state": "none"}
        self.reload_banks()
        self.refresh_work_controls()

    def rename_work_bank(self):
        keyboard = TouchKeyboard("Nom de la banque", self.work_bank.name, parent=self)
        if keyboard.exec() == QDialog.DialogCode.Accepted:
            name = keyboard.value().strip()
            if name:
                self.work_bank = replace(self.work_bank, name=name)
                self._initial_empty_draft = False
                self._update_dirty()

    def _mode_changed(self, index: int):
        if index < 0:
            return
        self.work_bank = replace(self.work_bank, mode=self.mode_combo.itemData(index))
        self._initial_empty_draft = False
        self._update_dirty()

    def _dirty(self) -> bool:
        if self.saved_bank is None:
            return not self._initial_empty_draft
        saved, work = self.saved_bank, self.work_bank
        camera = lambda bank: bank.camera.identity if bank.camera else None  # noqa: E731
        return ((work.name, work.mode, work.focus, camera(work))
                != (saved.name, saved.mode, saved.focus, camera(saved)))

    def _update_dirty(self):
        dirty = self._dirty()
        if self.saved_bank is None:
            label = "Nouvelle banque non sauvegardée" if dirty else "Brouillon vide"
        else:
            label = "Modifications non sauvegardées" if dirty else f"Banque sauvegardée · révision {self.saved_bank.revision}"
        self.work_title.setText(self.work_bank.name)
        self.work_title.setToolTip(self.work_bank.name)
        self.dirty_label.setText(label)
        self.dirty_label.setStyleSheet(f"color:{ORANGE_DARK};font-weight:600" if dirty else "")

    def _resolve_dirty(self) -> bool:
        if not self._dirty():
            return True
        choice = QMessageBox(self)
        choice.setWindowTitle("Modifications non sauvegardées")
        choice.setText(f"Que faire des modifications de la banque « {self.work_bank.name} » ?")
        save = choice.addButton("Sauvegarder", QMessageBox.ButtonRole.AcceptRole)
        discard = choice.addButton("Abandonner", QMessageBox.ButtonRole.DestructiveRole)
        stay = choice.addButton("Rester", QMessageBox.ButtonRole.RejectRole)
        choice.setDefaultButton(stay)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is save:
            return self.save_work_bank()
        if clicked is discard:
            self.work_bank = self.saved_bank or self._empty_draft()
            self._initial_empty_draft = self.saved_bank is None
            if self.work_bank.focus is not None:
                self._focus_report = {"value": self.work_bank.focus, "state": "not_applied"}
            else:
                self._focus_report = {"state": "none"}
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
                    self, "Remplacer la banque sauvegardée ?",
                    f"La banque « {self.saved_bank.name} » sera remplacée (révision suivante).",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                saved = self.store.save(self.work_bank, self.saved_bank.revision)
        except Exception as exc:
            self._error("Sauvegarde de la banque impossible", exc)
            return False
        self.saved_bank = self.work_bank = saved
        self._initial_empty_draft = False
        self._capture_bank_id = saved.id
        self.reload_banks()
        self.refresh_work_controls()
        return True

    # ------------------------------------------------------------------ tabs
    def _tab_changed(self, index: int):
        if self._changing_tab:
            return
        old = self.previous_tab
        if old == 0 and index != 0 and not self._resolve_dirty():
            self._changing_tab = True
            self.tabs.setCurrentIndex(old)
            self._changing_tab = False
            return
        self.previous_tab = index
        if index == 1:
            self.reload_banks()
            self._update_capture_bank()
        self._ensure_preview()

    # --------------------------------------------------------------- capture
    def _capture_bank_changed(self, index: int):
        self._capture_bank_id = self.capture_selector.itemData(index)
        self._update_capture_bank()

    def _update_capture_bank(self):
        bank_id = self._capture_bank_id
        ready = False
        try:
            bank = self.store.get(bank_id) if bank_id else None
            if bank is None:
                self.capture_summary.setText("Aucune banque sélectionnée.")
                self.capture_reason.setText("Choisissez une banque sauvegardée, ou préparez-en une dans Preview.")
            else:
                descriptor = MODES[bank.mode]
                focus = "non défini" if bank.focus is None else f"{bank.focus} / 1023"
                camera = bank.camera.name or bank.camera.path if bank.camera else "non définie"
                self.capture_summary.setText(
                    f"<div style='font-size:20px;font-weight:700'>{_escape(bank.name)}</div>"
                    f"<div><b>Mode d’acquisition</b> : {_escape(descriptor.label)}</div>"
                    f"<div><b>Focus manuel</b> {focus} <span style='color:#66717C'>(consigne)</span></div>"
                    f"<div><b>Caméra</b> {_escape(camera)}</div>"
                    f"<div style='color:#66717C'>Révision {bank.revision}</div>")
                missing = [label for label, value in (("Focus manuel", bank.focus), ("caméra", bank.camera))
                           if value is None]
                if missing:
                    self.capture_reason.setText(
                        f"Banque incomplète ({', '.join(missing)}) : complétez-la dans Preview puis sauvegardez.")
                else:
                    ready = True
                    self.capture_reason.setText("PHOTO utilise une copie figée de cette banque sauvegardée.")
                    if self.session_device is None or bank.camera.identity != self.session_device.identity:
                        ready = False
                        self.capture_reason.setText(
                            "Chargez cette banque dans Preview et sélectionnez sa caméra avant PHOTO.")
        except Exception as exc:
            self.capture_summary.setText("La banque sélectionnée est invalide.")
            self.capture_reason.setText(str(exc))
        if GATE.fault:
            self.capture_reason.setText("Caméra en défaut : redémarrez l’application.")
            ready = False
        elif ready and self.preview_worker is not None:
            self.capture_reason.setText("Libération du Preview en cours ; PHOTO sera disponible ensuite.")
            ready = False
        self.photo_button.setEnabled(ready and not self._busy_capture)

    def take_photo(self):
        if GATE.fault or self.preview_worker is not None or self._busy_capture:
            self._update_capture_bank()
            return
        if not self._capture_bank_id:
            self._update_capture_bank()
            return
        try:
            frozen = self.store.get(self._capture_bank_id)
            if frozen.focus is None or frozen.camera is None:
                raise ValueError("Cette banque ne définit pas de Focus manuel ou de caméra.")
            if self.session_device is None or frozen.camera.identity != self.session_device.identity:
                raise CameraSelectionError(
                    "La caméra de la banque ne correspond pas au contexte Preview. "
                    "Chargez cette banque dans Preview et sélectionnez sa caméra avant PHOTO.")
        except Exception as exc:
            self._error("Banque inutilisable", exc)
            return
        self._busy_capture = True
        self.photo_button.setEnabled(False)
        self.choose_folder_button.setEnabled(False)
        self.capture_progress.show()
        self.capture_state.setText("Préparation de la capture…")
        self._task = CaptureTask(frozen, self.photo_dir, self.session_device)
        self._task.signals.progress.connect(self.capture_state.setText)
        self._task.signals.progress.connect(self.preview_details.append)
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
        release = f"\n⚠ {output['release_error']}" if output.get("release_error") else ""
        if not output.get("image"):
            self.capture_state.setText(f"Original conservé : {output['original']}{release}")
            self.capture_image.setText("Rendu indisponible — original conservé")
            return
        readback = output.get("focus_readback")
        reading = "relecture indisponible" if readback is None else f"relecture {readback:g}"
        self.capture_state.setText(
            f"Capture enregistrée : {output['directory']}\n"
            f"Focus manuel {output['focus_requested']} — commande acceptée, {reading} ; "
            f"netteté non vérifiée.{release}"
        )
        image = output.get("preview")
        if image is not None:
            self._show_image(self.capture_image, image)

    @Slot(str)
    def _capture_failure(self, message: str):
        self._preview_failed = True
        self.preview_details.append(f"Échec capture : {message}. Utilisez « Relancer le Preview ».")
        self.capture_state.setText(f"Échec : {message}")
        QMessageBox.critical(self, "Capture impossible", message)

    def _capture_finished(self):
        self._busy_capture = False
        self.capture_progress.hide()
        self.choose_folder_button.setEnabled(True)
        if GATE.fault:
            self._enter_fault()
        self._update_capture_bank()
        if self._closing:
            self._finish_close()
            return
        self._ensure_preview()

    def _enter_fault(self):
        message = (f"Défaut caméra : {GATE.fault} La caméra n’est plus considérée comme disponible. "
                   "Redémarrez l’application. Les photos acquises sont conservées ; Traitement reste utilisable.")
        self.fault_banner.setText(message)
        self.fault_banner.show()
        for widget in (self.camera_combo, self.refresh_cameras_button, self.restart_preview_button,
                       self.photo_button, self.focus_resend):
            widget.setEnabled(False)
        self.preview_image.setText("Caméra en défaut — redémarrez l’application.")
        self.preview_status.setText("Caméra en défaut — redémarrez l’application.")

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

    # ------------------------------------------------------------ processing
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
        QMessageBox.critical(QApplication.activeWindow(), title, str(error))

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
        self._ensure_preview()
        if self._busy_capture or self._busy_save or self.preview_worker is not None:
            event.ignore()
            return
        event.accept()

    def _finish_close(self):
        if self.preview_worker is not None or self._busy_capture or self._busy_save:
            return
        self.close()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DataPeanuts.ArducamPhotoV2")
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("Arducam Photo")
    app.setWindowIcon(resources.window_icon())
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
