from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .config import Bank, MODES

RAW_BYTES = 108_000_000
RAW_TRANSPORT = (6000, 9000)
RAW_IMAGE_SHAPE = (9000, 12000)
_CAMERA_LOCK = threading.Lock()


class CameraError(RuntimeError):
    pass


class CameraOpenError(CameraError):
    pass


class CameraSetupError(CameraError):
    pass


class CameraReadError(CameraError):
    pass


class CameraBusyError(CameraError):
    pass


def _finite_readback(value) -> float | None:
    if isinstance(value, (int, float, np.number)) and np.isfinite(value):
        return float(value)
    return None


def _cv2():
    import cv2
    return cv2


def _open(index: int, backend: str):
    cv2 = _cv2()
    api = {"MSMF": cv2.CAP_MSMF, "DSHOW": cv2.CAP_DSHOW, "ANY": cv2.CAP_ANY}[backend]
    return cv2.VideoCapture(index, api)


def validate_raw(frame) -> np.ndarray:
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
        raise CameraReadError("Le buffer RAW reçu n’est pas un tableau uint8.")
    if frame.size != RAW_BYTES or frame.nbytes != RAW_BYTES:
        raise CameraReadError(
            f"Buffer RAW invalide : {frame.size} octets reçus, {RAW_BYTES} attendus "
            "(transport caméra 6000 × 9000)."
        )
    height, width = RAW_IMAGE_SHAPE
    transport_width, _transport_height = RAW_TRANSPORT
    if frame.shape not in ((RAW_BYTES,), (1, RAW_BYTES), (height, width),
                            (height, width, 1), (height, transport_width, 2)):
        raise CameraReadError(f"Disposition du buffer RAW non reconnue : {frame.shape}.")
    return frame.reshape(RAW_IMAGE_SHAPE).copy()


def validate_color(frame, mode: str) -> np.ndarray:
    expected = MODES[mode]
    if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
            or frame.shape != (expected.height, expected.width, 3)
            or frame.nbytes != expected.height * expected.width * 3):
        shape = getattr(frame, "shape", None)
        dtype = getattr(frame, "dtype", None)
        raise CameraReadError(
            f"Image couleur invalide ({shape}, {dtype}) ; {expected.width} × {expected.height}, BGR uint8 attendus."
        )
    return frame.copy()


@dataclass
class Acquisition:
    original: np.ndarray
    mode: str
    focus_requested: int
    focus_accepted: bool | None
    focus_readback: float | None
    received_shape: tuple[int, ...]
    dtype: str
    frames_read: int
    failed_reads: int
    timings: dict[str, float]
    backend: str
    camera_index: int
    capture_id: str
    released: bool
    settings: dict[str, object] = field(default_factory=dict)
    release_error: str | None = None


def _setup(cap, mode: str, focus: int, cv2):
    descriptor = MODES[mode]
    settings: dict[str, object] = {}
    width, height = RAW_TRANSPORT if descriptor.raw else (descriptor.width, descriptor.height)
    required = [(cv2.CAP_PROP_FRAME_WIDTH, width, "largeur"),
                (cv2.CAP_PROP_FRAME_HEIGHT, height, "hauteur")]
    if descriptor.raw:
        required.append((cv2.CAP_PROP_CONVERT_RGB, 0, "conversion couleur RAW"))
    for prop, value, label in required:
        try:
            accepted = bool(cap.set(prop, value))
        except Exception as exc:
            raise CameraSetupError(f"Impossible de configurer {label} : {exc}") from exc
        try:
            actual = cap.get(prop)
            readback_error = None
        except Exception as exc:
            actual = None
            readback_error = str(exc)
        readback = _finite_readback(actual)
        if actual is not None and readback is None and readback_error is None:
            readback_error = "relecture non numérique ou non finie"
        settings[label] = {"requested": value, "accepted": accepted, "readback": readback,
                           "readback_error": readback_error}
        if not accepted:
            raise CameraSetupError(f"Le pilote a refusé {label}={value}. Aucun autre mode ne sera essayé.")
    try:
        accepted = bool(cap.set(cv2.CAP_PROP_FOCUS, focus))
    except Exception as exc:
        settings["focus"] = {"requested": focus, "accepted": None, "readback": None,
                             "error": str(exc)}
        return settings, False, None, f"Erreur de commande focus : {exc}"
    try:
        readback = cap.get(cv2.CAP_PROP_FOCUS)
        readback_error = None
    except Exception as exc:
        readback = None
        readback_error = str(exc)
    normalized_readback = _finite_readback(readback)
    if readback is not None and normalized_readback is None and readback_error is None:
        readback_error = "relecture non numérique ou non finie"
    settings["focus"] = {"requested": focus, "accepted": accepted, "readback": normalized_readback,
                         "readback_error": readback_error}
    return settings, accepted, normalized_readback, None


def acquire(bank: Bank, capture_id: str, *, opener=None, stabilizing_reads: int = 5) -> Acquisition:
    bank.validate()
    if bank.focus is None:
        raise CameraSetupError("La bank ne définit pas de consigne de focus.")
    if not _CAMERA_LOCK.acquire(blocking=False):
        raise CameraBusyError("La caméra est déjà utilisée par le Preview ou une autre capture.")
    cap = None
    started = time.monotonic()
    frames = failed = 0
    timings = {}
    released = False
    failure = None
    result = None
    release_error = None
    try:
        cap = (opener or _open)(bank.camera_index, bank.backend)
        if cap is None or not cap.isOpened():
            raise CameraOpenError(
                f"Caméra inaccessible (index {bank.camera_index}, backend {bank.backend}). "
                "Vérifiez le câble USB et fermez les applications qui l’utilisent."
            )
        cv2 = _cv2()
        settings, accepted, readback, detail = _setup(cap, bank.mode, bank.focus, cv2)
        if not accepted:
            raise CameraSetupError(detail or "Le pilote a refusé la consigne de focus.")
        setup_done = time.monotonic()
        validator = validate_raw if MODES[bank.mode].raw else lambda f: validate_color(f, bank.mode)
        wanted = stabilizing_reads + 1
        last = None
        while frames < wanted:
            try:
                ok, frame = cap.read()
            except Exception as exc:
                raise CameraReadError(f"Erreur de lecture caméra : {exc}") from exc
            if not ok:
                failed += 1
                if failed > 5:
                    raise CameraReadError("La caméra n’a pas fourni d’image après plusieurs tentatives.")
                continue
            last = validator(frame)
            frames += 1
        read_done = time.monotonic()
        shape = tuple(frame.shape)
        timings.update(setup_s=setup_done - started, read_s=read_done - setup_done)
        result = Acquisition(last, bank.mode, bank.focus, accepted, readback, shape,
                             str(frame.dtype), frames + failed, failed, timings,
                             bank.backend, bank.camera_index, capture_id, released, settings)
    except BaseException as exc:
        failure = exc
    finally:
        if cap is not None:
            try:
                cap.release()
                released = True
            except Exception as exc:
                release_error = f"Impossible de libérer la caméra : {exc}"
                if result is None:
                    failure = CameraSetupError(release_error)
        timings["release_s"] = time.monotonic() - started - sum(timings.values())
        timings["total_s"] = time.monotonic() - started
        _CAMERA_LOCK.release()
    if failure is not None:
        raise failure
    result.released = released
    result.release_error = release_error
    return result
