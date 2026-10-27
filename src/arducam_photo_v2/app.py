from __future__ import annotations

import json
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QScroller, QSizePolicy, QSlider,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionSlider, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import resources
from .banks import BankStore
from .capture import Sequence, execute_sequence
from .camera import (GATE, MAX_STABILIZATION_ATTEMPTS, CameraError, CameraReadError,
                     CameraSelectionError, _cv2, apply_focus, list_devices, open_device,
                     release_device, resolve_device, validate_color)
from .config import DEFAULT_BACKEND, FOCUS_MAX, FOCUS_MIN, MODES, Bank, CameraDevice
from .paths import default_photo_dir
from .processing import reduce_image

ORANGE = "#FF6F4C"
ORANGE_DARK = "#B84228"
INK = "#20252B"
STAGE = "#222A31"

APP_STYLE = f"""
QWidget {{ background: #F6F5F3; color: {INK}; font-family: 'Segoe UI'; font-size: 15px; }}
QTabWidget::pane {{ border: 0; }}
QTabWidget::tab-bar {{ left: 0; }}
QTabBar {{ border: 0; }}
QTabBar::tab {{ min-height: 48px; min-width: 100px; padding: 0 18px; margin-right: 4px; border: 0;
  border-bottom: 4px solid transparent; color: #59636D; font-size: 17px; }}
QTabBar::tab:selected {{ color: {INK}; border-bottom-color: {ORANGE}; background: white; font-weight: 600; }}
QPushButton, QComboBox, QLineEdit {{ min-height: 48px; min-width: 24px; padding: 0 12px;
  border: 1px solid #D5D9DE; border-radius: 9px; background: white; }}
QPushButton {{ font-size: 16px; }}
QPushButton:hover, QComboBox:hover {{ border-color: {ORANGE}; }}
QPushButton:pressed {{ border: 2px inset #66717C; background: #E5E8EB; padding-top: 2px; }}
QPushButton:checked {{ border: 2px solid {INK}; background: #FFE1D8; font-weight: 600; }}
QPushButton:focus, QComboBox:focus, QLineEdit:focus {{ outline: none; border: 2px solid {INK}; }}
QPushButton:disabled, QComboBox:disabled {{ color: #66717C; background: #E8E5E1; border: 1px dashed #B8BDC2; }}
QPushButton#primary {{ color: white; border: 2px solid transparent; background: {ORANGE_DARK}; }}
QPushButton#primary:hover {{ background: #A63B24; }}
QPushButton#photo {{ min-height: 60px; font-size: 17px; color: white; border: 2px solid transparent;
  background: {ORANGE_DARK}; }}
QPushButton#photo:hover {{ background: #A63B24; }}
QPushButton#primary:focus, QPushButton#photo:focus {{ border: 2px solid {INK}; }}
QPushButton#primary:pressed, QPushButton#photo:pressed {{ background: #87301D; border: 2px inset white; }}
QPushButton#primary:disabled, QPushButton#photo:disabled {{
  color: #59636D; background: #E8E5E1; border: 2px dashed #B8BDC2; }}
QComboBox::drop-down {{ border: 0; width: 34px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url("{resources.file_path(resources.CHEVRON)}"); width: 22px; height: 22px; }}
QComboBox QAbstractItemView {{ min-height: 44px; selection-background-color: #FFE1D8; selection-color: {INK}; }}
QComboBox QAbstractItemView::item {{ min-height: 48px; padding: 0 12px; }}
QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{ height: 12px; background: #DCE0E4; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: #FFB4A1; border-radius: 6px; }}
QSlider::handle:horizontal {{ width: 44px; height: 44px; margin: -16px 0; border-radius: 22px; background: {ORANGE}; }}
QSlider::handle:horizontal:disabled {{ background: #C9CED3; }}
QProgressBar {{ min-height: 14px; max-height: 14px; border: 0; border-radius: 7px; background: #E0E4E8; }}
QProgressBar::chunk {{ background: {ORANGE}; border-radius: 7px; }}
QFrame#card {{ background: white; border: 1px solid #E6E3DF; border-radius: 14px; }}
QFrame#card QLabel {{ background: transparent; }}
QLabel#heading {{ font-size: 20px; font-weight: 600; }}
QLabel#muted {{ color: #59636D; }}
QLabel#value {{ font-size: 28px; font-weight: 600; color: {INK}; }}
QLabel#fault {{ color: white; background: #B3261E; border-radius: 8px; padding: 8px 12px; font-weight: 600; }}
QTextEdit {{ border: 1px solid #D5D9DE; border-radius: 8px; background: white; }}
QListWidget {{ border: 1px solid #D5D9DE; border-radius: 8px; background: white; }}
QListWidget::item {{ min-height: 56px; padding: 6px; }}
QListWidget::item:selected {{ background: #FFE1D8; color: {INK}; }}
QListWidget::indicator {{ width: 28px; height: 28px; }}
QScrollBar:vertical {{ width: 24px; }}
QScrollBar:horizontal {{ height: 24px; }}
QScrollArea {{ border: 0; }}
"""

TOUCH_MIN = 48


def _ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app else None
    return screen.devicePixelRatio() if screen else 1.0


def _button(text: str, *, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setMinimumSize(TOUCH_MIN, TOUCH_MIN)
    if primary:
        button.setObjectName("primary")
    return button


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


def _combo() -> QComboBox:
    combo = QComboBox()
    combo.setItemDelegate(QStyledItemDelegate(combo))
    return combo


def _scroll_area() -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)
    return scroll


def _muted(text: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(wrap)
    return label


def to_qimage(image: np.ndarray) -> QImage:
    array = np.ascontiguousarray(image)
    fmt = QImage.Format.Format_Grayscale8 if array.ndim == 2 else QImage.Format.Format_BGR888
    return QImage(array.data, array.shape[1], array.shape[0], array.strides[0], fmt).copy()


def capture_preview(filename: Path) -> QImage:
    # Decode bytes to avoid filename handling differences; full-resolution pixels are allocated.
    payload = np.frombuffer(filename.read_bytes(), dtype=np.uint8)
    try:
        image = cv2.imdecode(payload, cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise OSError(f"Lecture du rendu impossible : {filename} — {exc}") from exc
    if image is None:
        raise OSError(f"Lecture du rendu impossible : {filename}")
    del payload
    if max(image.shape[:2]) > 1600:
        image = reduce_image(image, 1600)
    preview = to_qimage(image)
    del image
    if preview.isNull():
        raise MemoryError("Impossible d’allouer l’image d’affichage.")
    return preview


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
        self.setMinimumWidth(max(440, layout.minimumSize().width()))

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
        self._focus_lock = threading.Lock()
        self._latest_focus: tuple[int, int] | None = None
        self.release_error: str | None = None
        self.terminal_error: str | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self):
        self._stop.set()

    def set_focus(self, sequence: int, value: int):
        with self._focus_lock:
            if self._latest_focus is None or sequence >= self._latest_focus[0]:
                self._latest_focus = (sequence, value)

    def take_unapplied(self) -> tuple[int, int] | None:
        with self._focus_lock:
            latest, self._latest_focus = self._latest_focus, None
            return latest

    def _apply_pending(self, cap, cv2_module):
        latest = self.take_unapplied()
        if latest is None or self._stop.is_set():
            if latest is not None:
                self.set_focus(*latest)
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
    def __init__(self, sequence: Sequence, photo_dir: Path):
        super().__init__()
        self.sequence = sequence
        self.photo_dir = photo_dir.resolve()
        self.signals = TaskSignals()

    def _completed(self, output: dict):
        if output.get("image"):
            try:
                output["preview"] = capture_preview(output["image"])
                output["thumbnail"] = output["preview"].scaled(
                    128, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            except (OSError, cv2.error, MemoryError, RuntimeError) as exc:
                output["display_error"] = str(exc)
        self.signals.success.emit(output)

    @Slot()
    def run(self):
        try:
            execute_sequence(self.sequence, self.photo_dir,
                             progress=self.signals.progress.emit, completed=self._completed)
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class PhotoTask(QRunnable):
    def __init__(self, filename: Path):
        super().__init__()
        self.filename = filename
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            self.signals.success.emit(capture_preview(self.filename))
        except Exception as exc:
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()


class BankDialog(QDialog):
    def __init__(self, bank: Bank, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Nom"))
        row = QHBoxLayout()
        self.name = QLineEdit(bank.name)
        row.addWidget(self.name, 1)
        keyboard = _button("Clavier")
        keyboard.clicked.connect(self.enter_name)
        row.addWidget(keyboard)
        layout.addLayout(row)
        layout.addWidget(QLabel(f"Focus manuel : {bank.focus} (consigne)"))
        layout.addWidget(QLabel("Mode d’acquisition photo"))
        self.mode = _combo()
        for key, mode in MODES.items():
            self.mode.addItem(mode.label, key)
        self.mode.setCurrentIndex(self.mode.findData(bank.mode))
        layout.addWidget(self.mode)
        layout.addWidget(_muted(
            "Ce choix ne change pas le Preview 720p. La netteté peut différer entre modes : "
            "la photo constitue le contrôle final."))
        row = QHBoxLayout()
        save = _button("Enregistrer", primary=True)
        save.clicked.connect(self.accept)
        cancel = _button("Annuler")
        cancel.clicked.connect(self.reject)
        row.addWidget(save)
        row.addWidget(cancel)
        layout.addLayout(row)

    def enter_name(self):
        keyboard = TouchKeyboard("Nom de la banque", self.name.text(), parent=self)
        if keyboard.exec() == QDialog.DialogCode.Accepted:
            self.name.setText(keyboard.value())

    def accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Nom manquant", "Saisissez un nom de banque.")
            return
        super().accept()


class BankCheckList(QListWidget):
    """The entire row toggles a bank, not only the checkbox indicator."""

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        self._pressed_item = item
        self._pressed_state = item.checkState() if item else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and item is getattr(self, "_pressed_item", None) and item:
            item.setCheckState(Qt.CheckState.Unchecked if self._pressed_state == Qt.CheckState.Checked
                               else Qt.CheckState.Checked)


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
        self._capture_bank_ids: set[str] = set()
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
        self._task: QRunnable | None = None
        self._last_capture: dict | None = None
        self._capture_results: list[dict] = []
        self._sequence_error: str | None = None
        self._display_generation = 0
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
        self.banks_page = self._build_banks()
        self.capture_page = self._build_capture()
        self.tabs.addTab(self.preview_page, "Preview")
        self.tabs.addTab(self.banks_page, "Banques")
        self.tabs.addTab(self.capture_page, "Capture")
        self.tabs.currentChanged.connect(self._tab_changed)
        outer.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(4, 0, 0, 0)
        self.notice = _muted()
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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        page_layout.setSpacing(8)
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        stage = QVBoxLayout()
        stage.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.camera_combo = _combo()
        self.camera_combo.setAccessibleName("Caméra")
        self.camera_combo.setToolTip("Caméra utilisée par le Preview et enregistrée dans la banque")
        self.camera_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.camera_combo.setMinimumContentsLength(10)
        self.camera_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.camera_combo.activated.connect(self._camera_chosen)
        self.refresh_cameras_button = _button("Actualiser")
        self.refresh_cameras_button.setToolTip("Actualiser la liste des caméras")
        self.refresh_cameras_button.clicked.connect(self.refresh_devices)
        self.work_selector = _combo()
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
        page_layout.addLayout(toolbar)
        self.preview_image = ImageView("Recherche des caméras…")
        stage.addWidget(self.preview_image, 1)
        self.preview_status = _muted("Aperçu de cadrage inactif.")
        stage.addWidget(self.preview_status)
        self.preview_disclaimer = _muted(
            "Aperçu de cadrage 720p : le mode et le champ de vue de la photo peuvent différer.",
            wrap=True)
        stage.addWidget(self.preview_disclaimer)
        layout.addLayout(stage, 1)

        self.preview_panel = QStackedWidget()
        self.preview_panel.setFixedWidth(360)
        controls_scroll = _scroll_area()
        controls_scroll.setWidget(self._build_preview_controls())
        self.preview_panel.addWidget(controls_scroll)
        self.preview_panel.addWidget(self._build_preview_details())
        layout.addWidget(self.preview_panel)
        page_layout.addLayout(layout, 1)
        return page

    def _build_preview_controls(self):
        card = _card()
        form = QVBoxLayout(card)
        form.setContentsMargins(14, 10, 14, 12)
        form.setSpacing(7)
        form.setAlignment(Qt.AlignmentFlag.AlignTop)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.work_title = QLabel()
        self.work_title.setObjectName("heading")
        self.work_title.setWordWrap(True)
        self.dirty_label = _muted()
        titles.addWidget(self.work_title)
        head.addLayout(titles, 1)
        self.preview_details_toggle = _button("Diagnostic")
        self.preview_details_toggle.setToolTip("Explications, identité caméra et diagnostic")
        self.preview_details_toggle.clicked.connect(lambda: self.preview_panel.setCurrentIndex(1))
        head.addWidget(self.preview_details_toggle)
        form.addLayout(head)
        self.dirty_label.setMinimumHeight(40)
        form.addWidget(self.dirty_label)
        form.addWidget(QLabel("Focus manuel"))
        focus_row = QHBoxLayout()
        focus_row.setSpacing(6)
        self.focus_minus = _button("−")
        self.focus_plus = _button("+")
        for button, label in ((self.focus_minus, "Focus manuel −1"), (self.focus_plus, "Focus manuel +1")):
            button.setFixedWidth(56)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(450)
            button.setAutoRepeatInterval(100)
            button.setToolTip(label)
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
        self.focus_command_status.setMinimumHeight(80)
        self.focus_resend = _button("Renvoyer")
        self.focus_resend.setToolTip("Renvoyer la même consigne de Focus manuel au Preview")
        self.focus_resend.clicked.connect(self.resend_focus)
        status_row.addWidget(self.focus_command_status, 1)
        status_row.addWidget(self.focus_resend)
        form.addLayout(status_row)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.save_bank_button = _button("Sauvegarder le focus", primary=True)
        self.save_bank_button.clicked.connect(self.save_work_bank)
        self.new_bank_button = _button("Créer une banque avec ce focus")
        self.new_bank_button.clicked.connect(self.create_draft)
        form.addWidget(self.save_bank_button)
        actions.addWidget(self.new_bank_button, 1)
        form.addLayout(actions)
        return card

    def _build_preview_details(self):
        card = _card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(14, 10, 14, 12)
        back = _button("← Réglages")
        back.clicked.connect(lambda: self.preview_panel.setCurrentIndex(0))
        self.preview_details_back = back
        outer.addWidget(back)
        self.restart_preview_button = _button("Relancer le Preview")
        self.restart_preview_button.clicked.connect(self.restart_preview)
        outer.addWidget(self.restart_preview_button)
        scroll = _scroll_area()
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
        self.preview_details = QTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setMinimumHeight(120)
        self.preview_details.setPlaceholderText("Les rapports caméra apparaîtront ici.")
        self.preview_details.document().setMaximumBlockCount(200)
        column.addWidget(self.preview_details, 1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        return card

    def _build_banks(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_muted(
            "Configurations de prise de vues de cartes, composants et conditionnements électroniques. "
            "Une consigne commune ne garantit pas une netteté identique entre modes."))
        self.bank_list = QListWidget()
        self.bank_list.setWordWrap(True)
        QScroller.grabGesture(self.bank_list.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        self.bank_list.setAccessibleName("Banques sauvegardées")
        layout.addWidget(self.bank_list, 1)
        actions = QHBoxLayout()
        self.edit_bank_button = _button("Nom et mode")
        self.edit_bank_button.clicked.connect(self.edit_bank)
        self.duplicate_bank_button = _button("Dupliquer")
        self.duplicate_bank_button.clicked.connect(self.duplicate_bank)
        self.edit_focus_button = _button("Régler le focus dans Preview")
        self.edit_focus_button.clicked.connect(self.edit_bank_focus)
        self.delete_bank_button = _button("Supprimer")
        self.delete_bank_button.clicked.connect(self.delete_bank)
        for button in (self.edit_bank_button, self.duplicate_bank_button,
                       self.edit_focus_button, self.delete_bank_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        return page

    def _build_capture(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        card = _card()
        left = QVBoxLayout(card)
        left.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        left.setContentsMargins(14, 10, 14, 12)
        left.setSpacing(8)
        capture_heading = QLabel("Capture")
        capture_heading.setObjectName("heading")
        header = QHBoxLayout()
        header.addWidget(capture_heading, 1)
        self.capture_diagnostic_button = _button("Diagnostic")
        self.capture_diagnostic_button.clicked.connect(self.show_diagnostic)
        header.addWidget(self.capture_diagnostic_button)
        left.addLayout(header)
        self.capture_selector = BankCheckList()
        QScroller.grabGesture(self.capture_selector.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        self.capture_selector.setAccessibleName("Banques à capturer dans l’ordre affiché")
        self.capture_selector.setWordWrap(True)
        self.capture_selector.setMinimumHeight(124)
        self.capture_selector.setMaximumHeight(124)
        self.capture_selector.itemChanged.connect(self._capture_selection_changed)
        left.addWidget(self.capture_selector)
        self.capture_summary = QLabel("0 banques sélectionnées = 0 photos")
        self.capture_summary.setWordWrap(True)
        self.capture_summary.setTextFormat(Qt.TextFormat.RichText)
        left.addWidget(self.capture_summary)
        self.object_id = QLineEdit()
        self.object_id.setPlaceholderText("ID de l’objet (facultatif)")
        self.object_id.setAccessibleName("ID de l’objet")
        self.object_description = QLineEdit()
        self.object_description.setPlaceholderText("Description de l’objet (facultative)")
        self.object_description.setAccessibleName("Description de l’objet")
        for title, field in (("ID de l’objet", self.object_id),
                             ("Description de l’objet", self.object_description)):
            row = QHBoxLayout()
            label = QLabel(title)
            label.setWordWrap(True)
            label.setFixedWidth(96)
            label.setMinimumHeight(48)
            label.setBuddy(field)
            row.addWidget(label)
            row.addWidget(field, 1)
            left.addLayout(row)
        self.clear_object_button = _button("Effacer les informations objet")
        self.clear_object_button.clicked.connect(self.clear_object_information)
        left.addWidget(self.clear_object_button)
        self.photo_button = _button("Capturer", primary=True)
        self.photo_button.setObjectName("photo")
        self.photo_button.setIcon(resources.squirrel_icon())
        self.photo_button.setIconSize(QSize(26, 26))
        self.photo_button.clicked.connect(self.take_photo)
        left.addWidget(self.photo_button)
        self.capture_progress = QProgressBar()
        self.capture_progress.setRange(0, 0)
        self.capture_progress.setTextVisible(False)
        self.capture_progress.hide()
        left.addWidget(self.capture_progress)
        self.capture_reason = _muted()
        self.capture_state = _muted("Aucune opération en cours.")
        self.capture_state.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        state_scroll = _scroll_area()
        state_scroll.setMinimumHeight(48)
        state_scroll.setMaximumHeight(60)
        state_body = QWidget()
        state_layout = QVBoxLayout(state_body)
        state_layout.setContentsMargins(0, 0, 4, 0)
        state_layout.addWidget(self.capture_reason)
        state_layout.addWidget(self.capture_state)
        state_layout.addStretch(1)
        state_scroll.setWidget(state_body)
        left.addWidget(state_scroll)
        left.addStretch(1)
        self.photo_path_label = _muted(str(self.photo_dir))
        self.photo_path_label.hide()
        folder_row = QHBoxLayout()
        self.choose_folder_button = _button("Dossier…")
        self.choose_folder_button.clicked.connect(self.choose_photo_dir)
        self.open_folder_button = _button("Ouvrir")
        self.open_folder_button.setToolTip("Ouvrir le dossier des photos")
        self.open_folder_button.clicked.connect(self.open_photo_folder)
        self.open_last_button = _button("Photo")
        self.open_last_button.setToolTip("Ouvrir la photo sélectionnée")
        self.open_last_button.clicked.connect(self.open_last_image)
        self.open_last_button.setEnabled(False)
        folder_row.addWidget(self.choose_folder_button)
        folder_row.addWidget(self.open_folder_button)
        folder_row.addWidget(self.open_last_button)
        left.addLayout(folder_row)
        controls_scroll = _scroll_area()
        controls_scroll.setFixedWidth(360)
        controls_scroll.setWidget(card)
        layout.addWidget(controls_scroll)
        results = QVBoxLayout()
        self.capture_image = ImageView("Aucune image capturée")
        results.addWidget(self.capture_image, 1)
        self.result_list = QListWidget()
        QScroller.grabGesture(self.result_list.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        self.result_list.setAccessibleName("Résultats de la séquence")
        self.result_list.setIconSize(QSize(128, 80))
        self.result_list.setMaximumHeight(180)
        self.result_list.currentRowChanged.connect(self.select_result)
        results.addWidget(self.result_list)
        layout.addLayout(results, 1)
        return page

    def show_diagnostic(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Diagnostic")
        dialog.resize(640, 440)
        layout = QVBoxLayout(dialog)
        log = QTextEdit()
        log.setReadOnly(True)
        log.setPlainText(self.preview_details.toPlainText() or "Aucun rapport caméra.")
        layout.addWidget(log, 1)
        close = _button("Fermer")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

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
            self._focus_seq += 1
            worker.set_focus(self._focus_seq, self.work_bank.focus)
            self._focus_report = {"seq": self._focus_seq, "value": self.work_bank.focus, "state": "sent"}
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
            if message.startswith(("Backend choisi :", "Dimensions réellement reçues :")):
                return
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
        if self.tabs.currentIndex() == 2:
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
            text = f"Consigne {value} · erreur pilote (Diagnostic)."
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
        return Bank.draft("Focus de travail", "color_720p", 0)

    def reload_banks(self):
        """One stable order is shared by the check list and sequence."""
        banks = self.store.list()
        known = {bank.id for bank in banks}
        self._capture_bank_ids.intersection_update(known)
        selected = self.bank_list.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.work_selector.blockSignals(True)
        self.work_selector.clear()
        self.work_selector.addItem("Régler une banque…", None)
        self.bank_list.clear()
        self.capture_selector.blockSignals(True)
        self.capture_selector.clear()
        for position, bank in enumerate(banks, 1):
            self.work_selector.addItem(bank.name, bank.id)
            camera = bank.camera.name or bank.camera.path if bank.camera else "non définie"
            item = QListWidgetItem(
                f"{bank.name}\n{MODES[bank.mode].label} · Focus {bank.focus}\n"
                f"Caméra : {camera} · Révision {bank.revision}")
            item.setData(Qt.ItemDataRole.UserRole, bank.id)
            if bank.camera:
                item.setToolTip(
                    f"{bank.camera.backend} · index résolu à l’ouverture\n"
                    f"{bank.camera.path or 'Chemin indisponible'}")
            self.bank_list.addItem(item)
            if bank.id == selected_id:
                self.bank_list.setCurrentItem(item)
            capture_item = QListWidgetItem(
                f"{position}. {bank.name}\n{bank.mode} · Focus {bank.focus}")
            capture_item.setData(Qt.ItemDataRole.UserRole, bank.id)
            capture_item.setFlags(capture_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            capture_item.setCheckState(Qt.CheckState.Checked if bank.id in self._capture_bank_ids
                                       else Qt.CheckState.Unchecked)
            self.capture_selector.addItem(capture_item)
        bank_id = self.saved_bank.id if self.saved_bank else None
        self.work_selector.setCurrentIndex(max(0, self.work_selector.findData(bank_id)))
        self.work_selector.blockSignals(False)
        self.capture_selector.blockSignals(False)
        self._update_capture_bank()

    def refresh_work_controls(self):
        self._focus_sync()
        self._update_dirty()
        self.save_bank_button.setVisible(self.saved_bank is not None)

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
        if bank.focus is not None:
            if self.preview_worker and self.preview_worker.device.identity != (
                    self.session_device.identity if self.session_device else None):
                self.stop_preview()
            self._send_focus(bank.focus)
        self._ensure_preview()

    def create_draft(self):
        if not self._focus_can_save(confirm_readback=False):
            return
        draft = Bank.draft("Nouvelle banque", self.work_bank.mode, self.work_bank.focus,
                           self.session_device)
        focus_sequence = self._focus_seq
        dialog = BankDialog(draft, "Créer une banque avec ce focus", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if (focus_sequence != self._focus_seq or draft.focus != self.work_bank.focus
                or draft.camera != self.session_device):
            QMessageBox.warning(self, "Focus modifié", "La consigne ou la caméra a changé. Rouvrez le formulaire.")
            return
        if not self._focus_can_save():
            return
        try:
            self.store.create(replace(draft, name=dialog.name.text().strip(),
                                      mode=dialog.mode.currentData()))
        except Exception as exc:
            self._error("Création impossible", exc)
            return
        self.reload_banks()
        self.notice.setText("Banque créée. Aucune photo déclenchée.")

    def _selected_bank(self) -> Bank | None:
        item = self.bank_list.currentItem()
        if item is None:
            self.notice.setText("Sélectionnez une banque.")
            return None
        return self.store.get(item.data(Qt.ItemDataRole.UserRole))

    def edit_bank(self):
        self._bank_form(duplicate=False)

    def duplicate_bank(self):
        self._bank_form(duplicate=True)

    def _bank_form(self, *, duplicate: bool):
        try:
            bank = self._selected_bank()
            if bank is None:
                return
            draft = replace(bank, name=f"{bank.name} — copie") if duplicate else bank
            dialog = BankDialog(draft, "Dupliquer une banque" if duplicate else "Nom et mode", self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            name, mode = dialog.name.text().strip(), dialog.mode.currentData()
            if duplicate:
                self.store.duplicate(bank, name, mode)
            else:
                saved = self.store.save(replace(bank, name=name, mode=mode), bank.revision)
                if self.saved_bank and self.saved_bank.id == saved.id:
                    self.saved_bank = self.work_bank = saved
                    self.refresh_work_controls()
        except Exception as exc:
            self._error("Modification impossible", exc)
            return
        self.reload_banks()

    def edit_bank_focus(self):
        try:
            bank = self._selected_bank()
            if bank is None:
                return
            index = self.work_selector.findData(bank.id)
            self._work_selected(index)
            if self.saved_bank and self.saved_bank.id == bank.id:
                self.tabs.setCurrentIndex(0)
        except Exception as exc:
            self._error("Chargement impossible", exc)

    def delete_bank(self):
        try:
            bank = self._selected_bank()
            if bank is None:
                return
            if QMessageBox.question(
                    self, "Supprimer la banque ?",
                    f"Supprimer « {bank.name} » ? Les photos, RAW et JSON restent inchangés.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
            self.store.delete(bank.id)
            if self.saved_bank and self.saved_bank.id == bank.id:
                self.saved_bank = None
                self.work_bank = replace(self.work_bank, id=str(uuid.uuid4()), name="Focus de travail")
                self.refresh_work_controls()
        except Exception as exc:
            self._error("Suppression impossible", exc)
            return
        self.reload_banks()

    def _focus_can_save(self, *, confirm_readback: bool = True) -> bool:
        report = self._focus_report
        if (self.focus_slider.isSliderDown() or report.get("state") != "accepted"
                or report.get("seq") != self._focus_seq
                or report.get("value") != self.work_bank.focus
                or self.session_device is None or not self._session_active()):
            QMessageBox.warning(
                self, "Focus non confirmé",
                "La consigne courante doit être acceptée dans le Preview actif avant sauvegarde. "
                "Attendez le pilote ou renvoyez la consigne.")
            return False
        reading = report["report"].get("readback")
        if confirm_readback and (reading is None or reading != self.work_bank.focus):
            return QMessageBox.question(
                self, "Limite de vérification du focus",
                ("Le pilote accepte la consigne mais ne permet pas de la vérifier par relecture. "
                 if reading is None else f"Le pilote accepte la consigne, mais relit {reading:g}. ")
                +
                "La netteté reste à contrôler sur la photo. Enregistrer cette consigne ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes
        return True

    def _dirty(self) -> bool:
        if self.saved_bank is None:
            return False
        saved, work = self.saved_bank, self.work_bank
        camera = lambda bank: bank.camera.identity if bank.camera else None  # noqa: E731
        return ((work.name, work.mode, work.focus, camera(work))
                != (saved.name, saved.mode, saved.focus, camera(saved)))

    def _update_dirty(self):
        dirty = self._dirty()
        if self.saved_bank is None:
            label = "Consigne de travail · aucune banque en édition"
        else:
            label = "Modifications à sauvegarder" if dirty else f"Sauvegardée · révision {self.saved_bank.revision}"
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
            if self.work_bank.focus is not None:
                self._focus_report = {"value": self.work_bank.focus, "state": "not_applied"}
            else:
                self._focus_report = {"state": "none"}
            self.refresh_work_controls()
            self._pending_focus = None
            if self.work_bank.focus is not None:
                self._send_focus(self.work_bank.focus)
            return True
        return False

    def save_work_bank(self) -> bool:
        if self.saved_bank is None or not self._focus_can_save():
            return False
        try:
            self.work_bank.validate()
            saved = self.store.save(self.work_bank, self.saved_bank.revision)
        except Exception as exc:
            self._error("Sauvegarde de la banque impossible", exc)
            return False
        self.saved_bank = self.work_bank = saved
        self.reload_banks()
        self.refresh_work_controls()
        return True

    # ------------------------------------------------------------------ tabs
    def _tab_changed(self, index: int):
        if self._changing_tab:
            return
        old = self.previous_tab
        if self._busy_capture or (old == 0 and index != 0 and not self._resolve_dirty()):
            self._changing_tab = True
            self.tabs.setCurrentIndex(old)
            self._changing_tab = False
            return
        self.previous_tab = index
        if index in (1, 2):
            self.reload_banks()
            self._update_capture_bank()
        self._ensure_preview()

    # --------------------------------------------------------------- capture
    def clear_object_information(self):
        self.object_id.clear()
        self.object_description.clear()

    def _capture_selection_changed(self, _item=None):
        if self._busy_capture:
            return
        self._capture_bank_ids = {
            self.capture_selector.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.capture_selector.count())
            if self.capture_selector.item(index).checkState() == Qt.CheckState.Checked}
        self._update_capture_bank()

    def _ordered_capture_banks(self) -> tuple[Bank, ...]:
        return tuple(bank for bank in self.store.list() if bank.id in self._capture_bank_ids)

    def _update_capture_bank(self):
        banks = self._ordered_capture_banks()
        count = len(banks)
        self.capture_summary.setText(f"{count} banques sélectionnées = {count} photos")
        ready = bool(banks) and all(bank.focus is not None and bank.camera is not None for bank in banks)
        message = "Une photo par banque, dans l’ordre affiché." if ready else "Cochez des banques complètes."
        if GATE.fault:
            ready, message = False, "Caméra en défaut : redémarrez l’application."
        elif self.preview_worker is not None:
            ready, message = False, "Libération du Preview en cours…"
        elif self._busy_capture:
            ready, message = False, "Séquence en cours."
        self.capture_reason.setText(message)
        self.photo_button.setEnabled(ready)

    def _set_capture_busy(self, busy: bool):
        self._busy_capture = busy
        for index in (0, 1):
            self.tabs.setTabEnabled(index, not busy)
        for widget in (self.capture_selector, self.object_id, self.object_description,
                       self.clear_object_button, self.choose_folder_button):
            widget.setEnabled(not busy)
        self.photo_button.setText("Capture en cours…" if busy else "Capturer")
        self.capture_progress.setVisible(busy)
        self._update_capture_bank()

    def take_photo(self):
        if self._busy_capture or GATE.fault or self.preview_worker is not None:
            self._update_capture_bank()
            return
        if self.tabs.currentIndex() != 2:
            return
        try:
            frozen_banks = self._ordered_capture_banks()
            if {bank.id for bank in frozen_banks} != self._capture_bank_ids:
                raise ValueError("Une banque sélectionnée a été supprimée. Actualisez la sélection.")
            sequence = Sequence(
                frozen_banks,
                self.object_id.text() if self.object_id.text().strip() else None,
                self.object_description.text() if self.object_description.text().strip() else None)
        except Exception as exc:
            self._error("Séquence inutilisable", exc)
            return
        self._display_generation += 1
        self._capture_results.clear()
        self._sequence_error = None
        self.result_list.clear()
        self._last_capture = None
        self.open_last_button.setEnabled(False)
        self.capture_image.setText("Séquence en cours…")
        self._set_capture_busy(True)
        self.capture_state.setText("Préparation de la séquence…")
        self._task = CaptureTask(sequence, self.photo_dir)
        self._task.signals.progress.connect(self.capture_state.setText)
        self._task.signals.progress.connect(self.preview_details.append)
        self._task.signals.success.connect(self._capture_success)
        self._task.signals.failure.connect(self._sequence_failure)
        self._task.signals.finished.connect(self._capture_finished)
        self._start_task(self._task)

    @Slot(str)
    def _sequence_failure(self, message: str):
        self._sequence_error = message
        self.preview_details.append(f"Erreur de séquence : {message}")
        self.capture_state.setText("Séquence interrompue — détails dans Diagnostic.")

    @Slot(object)
    def _capture_success(self, output: dict):
        output = output.copy()
        image = output.pop("preview", None)
        thumbnail = output.pop("thumbnail", None)
        if output.get("image") and (not isinstance(image, QImage) or image.isNull()):
            output.setdefault("display_error", "Aucune image d’affichage valide reçue.")
        self._capture_results.append(output)
        bank = output["bank"]
        statuses = {"complete": "Enregistrée", "render_error": "RAW conservé · rendu échoué",
                    "release_error": "Original conservé · défaut caméra",
                    "failed": "Échec", "not_executed": "Non exécutée"}
        status = statuses[output["status"]]
        if output.get("display_error"):
            status = "Photo enregistrée, affichage indisponible"
        output["label"] = status
        item = QListWidgetItem(f"{output['position']}. {bank.name} · {bank.mode}\n{status}")
        if isinstance(thumbnail, QImage) and not thumbnail.isNull():
            item.setIcon(QIcon(QPixmap.fromImage(thumbnail)))
        self.result_list.blockSignals(True)
        self.result_list.addItem(item)
        self.result_list.setCurrentItem(item)
        self.result_list.blockSignals(False)
        self._display_generation += 1
        self._last_capture = output
        self.open_last_button.setEnabled(bool(output.get("image")))
        if isinstance(image, QImage) and not image.isNull():
            self.capture_image.set_qimage(image)
        else:
            self.capture_image.setText(status)
        self.preview_details.append(
            f"Photo {output['position']} · {bank.name} · {status}\n"
            f"{output.get('error') or output.get('display_error') or output.get('manifest', '')}")

    def select_result(self, row: int):
        self._display_generation += 1
        generation = self._display_generation
        if not 0 <= row < len(self._capture_results):
            return
        output = self._capture_results[row]
        self._last_capture = output
        self.open_last_button.setEnabled(bool(output.get("image")))
        self.capture_image.setText(output["label"])
        if not output.get("image"):
            return
        task = PhotoTask(output["image"])
        task.signals.success.connect(
            lambda image: self.capture_image.set_qimage(image)
            if generation == self._display_generation else None)
        task.signals.failure.connect(
            lambda error: self._display_failure(error, generation))
        self._start_task(task)

    def _display_failure(self, error: str, generation: int):
        self.preview_details.append(f"Affichage : {error}")
        if generation == self._display_generation:
            self.capture_image.setText("Photo enregistrée, affichage indisponible")
            row = self.result_list.currentRow()
            if 0 <= row < len(self._capture_results):
                output = self._capture_results[row]
                output["display_error"] = error
                output["label"] = "Photo enregistrée, affichage indisponible"
                bank = output["bank"]
                self.result_list.item(row).setText(
                    f"{output['position']}. {bank.name} · {bank.mode}\n{output['label']}")

    @Slot()
    def _capture_finished(self):
        self.reload_banks()
        self._set_capture_busy(False)
        counts = {status: sum(item["status"] == status for item in self._capture_results)
                  for status in ("complete", "failed", "render_error", "release_error", "not_executed")}
        self.capture_state.setText(
            f"{counts['complete']} réussie(s) · "
            f"{counts['failed'] + counts['render_error'] + counts['release_error']} en échec · "
            f"{counts['not_executed']} non exécutée(s).")
        if self._sequence_error:
            self.capture_state.setText("Séquence interrompue — détails dans Diagnostic.")
        if GATE.fault:
            self._enter_fault()
        if self._closing:
            self._finish_close()

    def _start_task(self, task: QRunnable):
        self._active_tasks.append(task)
        task.signals.finished.connect(lambda task=task: self._task_finished(task))
        self.pool.start(task)

    def _task_finished(self, task: QRunnable):
        if task in self._active_tasks:
            self._active_tasks.remove(task)
        if self._closing:
            self._finish_close()

    def _enter_fault(self):
        message = (f"Défaut caméra : {GATE.fault} La caméra n’est plus considérée comme disponible. "
                   "Redémarrez l’application. Les photos acquises sont conservées.")
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
        if self._last_capture and self._last_capture.get("image"):
            filename = self._last_capture["image"]
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(filename)))

    def open_photo_folder(self):
        directory = (self._last_capture.get("directory") if self._last_capture else None) or self.photo_dir
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    @staticmethod
    def _error(title, error):
        QMessageBox.critical(QApplication.activeWindow(), title, str(error))

    def closeEvent(self, event: QCloseEvent):
        if self._busy_capture:
            activity = "séquence de capture"
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
        if self._busy_capture or self.preview_worker is not None or self._active_tasks:
            event.ignore()
            return
        event.accept()

    def _finish_close(self):
        if self.preview_worker is not None or self._busy_capture or self._active_tasks:
            return
        self.close()


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
