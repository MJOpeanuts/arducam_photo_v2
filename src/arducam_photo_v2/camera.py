from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .config import DEFAULT_BACKEND, Bank, CameraDevice, MODES

RAW_BYTES = 108_000_000
RAW_TRANSPORT = (6000, 9000)
RAW_IMAGE_SHAPE = (9000, 12000)
REQUIRED_VALID_FRAMES = 6
MAX_STABILIZATION_ATTEMPTS = 30


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


class CameraFaultError(CameraError):
    pass


class CameraSelectionError(CameraError):
    pass


class CameraStabilizationError(CameraReadError):
    def __init__(self, message: str, stats: dict):
        super().__init__(message)
        self.stats = stats


class CameraGate:
    """Single owner of the camera hardware for the whole process.

    A failed hardware release puts the gate in a permanent fault state: the
    software lock being free does not prove the device is available, so no new
    session may open until the application restarts.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._fault: str | None = None
        self.owner: str | None = None

    @property
    def fault(self) -> str | None:
        return self._fault

    def claim(self, owner: str) -> None:
        with self._state:
            if self._fault:
                raise CameraFaultError(
                    f"Caméra en défaut : {self._fault} Redémarrez l’application avant toute nouvelle ouverture."
                )
            if not self._lock.acquire(blocking=False):
                raise CameraBusyError(f"La caméra est déjà utilisée ({self.owner or 'autre session'}).")
            self.owner = owner

    def release(self, release_error: str | None = None) -> None:
        with self._state:
            if release_error:
                self._fault = release_error
            self.owner = None
            self._lock.release()

    def reset_for_tests(self) -> None:
        with self._state:
            self._fault = None
            self.owner = None
            if self._lock.locked():
                self._lock.release()


GATE = CameraGate()


def _finite_readback(value) -> float | None:
    if isinstance(value, (int, float, np.number)) and not isinstance(value, bool) and np.isfinite(value):
        return float(value)
    return None


def _cv2():
    import cv2
    return cv2


def _api(cv2, backend: str) -> int:
    return getattr(cv2, {"DSHOW": "CAP_DSHOW", "MSMF": "CAP_MSMF", "V4L2": "CAP_V4L2"}[backend])


def require_backend(cv2, backend: str) -> int:
    api = _api(cv2, backend)
    if not cv2.videoio_registry.hasBackend(api):
        raise CameraOpenError(
            f"Backend choisi : {backend} ; disponibilité : non. "
            "Ce backend est indisponible dans cet environnement OpenCV. Aucun repli ne sera essayé."
        )
    return api


def _enumerate(cv2, backend: str):
    from cv2_enumerate_cameras import enumerate_cameras
    return enumerate_cameras(_api(cv2, backend))


def list_devices(backend: str, *, enumerator=None) -> list[CameraDevice]:
    """Enumerate devices without opening them. Names may be unavailable."""
    cv2 = _cv2()
    require_backend(cv2, backend)
    infos = (enumerator or _enumerate)(cv2, backend)
    devices = []
    for info in infos:
        name = (getattr(info, "name", None) or "").strip() or None
        path = (getattr(info, "path", None) or "").strip() or None
        device = CameraDevice(backend, int(info.index), name, path)
        device.validate()
        devices.append(device)
    return devices


def resolve_device(expected: CameraDevice, devices: list[CameraDevice]) -> CameraDevice:
    """Find the expected hardware; never substitute another device."""
    candidates = [device for device in devices if device.backend == expected.backend]
    if devices and not candidates:
        raise CameraSelectionError(
            f"La caméra enregistrée utilise {expected.backend}, différent du backend choisi "
            f"({devices[0].backend}). Sélectionnez la caméra dans Preview puis sauvegardez la banque ; "
            "aucun index ne sera repris d’un autre backend."
        )
    if expected.path:
        matches = [device for device in candidates if device.path == expected.path]
    elif expected.name:
        matches = [device for device in candidates if device.name == expected.name]
    else:
        raise CameraSelectionError(
            "Caméra non identifiable (ni chemin ni nom) : sélectionnez-la dans Preview."
        )
    label = expected.name or expected.path
    if not matches:
        raise CameraSelectionError(
            f"Caméra attendue absente : {label}. Branchez-la ou sélectionnez une caméra dans Preview."
        )
    if len(matches) > 1:
        raise CameraSelectionError(
            f"Caméra ambiguë : {len(matches)} périphériques correspondent à {label}. "
            "Sélectionnez la bonne caméra dans Preview."
        )
    return matches[0]


def open_device(device: CameraDevice):
    cv2 = _cv2()
    return cv2.VideoCapture(device.index, require_backend(cv2, device.backend))


def release_device(cap) -> str | None:
    try:
        cap.release()
    except Exception as exc:
        return f"Impossible de libérer la caméra : {exc}"
    return None


def validate_raw(frame) -> np.ndarray:
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
        raise CameraReadError(
            f"Le buffer RAW reçu n’est pas un tableau uint8 : "
            f"forme={getattr(frame, 'shape', None)}, type={getattr(frame, 'dtype', None)}.")
    if frame.size != RAW_BYTES or frame.nbytes != RAW_BYTES:
        raise CameraReadError(
            f"Buffer RAW invalide : {frame.nbytes} octets reçus, {RAW_BYTES} attendus, "
            f"forme={frame.shape}, type={frame.dtype} "
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


def apply_focus(cap, focus: int, cv2) -> dict:
    """Send one focus command. Acceptance and readback are reported separately."""
    try:
        accepted = bool(cap.set(cv2.CAP_PROP_FOCUS, focus))
    except Exception as exc:
        return {"requested": focus, "accepted": None, "readback": None, "error": str(exc)}
    try:
        raw_readback = cap.get(cv2.CAP_PROP_FOCUS)
        readback_error = None
    except Exception as exc:
        raw_readback = None
        readback_error = str(exc)
    readback = _finite_readback(raw_readback)
    if raw_readback is not None and readback is None and readback_error is None:
        readback_error = "relecture non numérique ou non finie"
    return {"requested": focus, "accepted": accepted, "readback": readback, "readback_error": readback_error}


@dataclass
class Acquisition:
    original: np.ndarray
    mode: str
    focus_requested: int
    focus_accepted: bool | None
    focus_readback: float | None
    received_shape: tuple[int, ...]
    dtype: str
    stabilization: dict
    timings: dict[str, float]
    device: CameraDevice
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
            raise CameraSetupError(
                f"Étape configuration : le pilote a refusé {label}={value} ; "
                f"relecture={readback}, erreur de relecture={readback_error}. "
                "Aucun autre mode ne sera essayé.")
    settings["focus"] = apply_focus(cap, focus, cv2)
    return settings


def stabilize(cap, mode: str, *, required_valid: int = REQUIRED_VALID_FRAMES,
              max_attempts: int = MAX_STABILIZATION_ATTEMPTS):
    """Read until ``required_valid`` consecutive conforming frames arrive.

    Transient frames after a resolution change are rejected and counted. The
    loop is bounded by ``max_attempts`` reads; no other mode is ever tried.
    """
    if required_valid < 1 or max_attempts < required_valid:
        raise ValueError("Limites de stabilisation incohérentes.")
    descriptor = MODES[mode]
    validator = validate_raw if descriptor.raw else (lambda frame: validate_color(frame, mode))
    stats = {"required_valid": required_valid, "max_attempts": max_attempts, "attempts": 0,
             "failed_reads": 0, "invalid_frames": 0, "valid_frames": 0,
             "last_read_error": None, "last_invalid_reason": None}
    consecutive = 0
    last = received = None
    while stats["attempts"] < max_attempts:
        stats["attempts"] += 1
        try:
            ok, frame = cap.read()
        except Exception as exc:
            ok, frame = False, None
            stats["last_read_error"] = str(exc)
        if not ok or frame is None:
            stats["failed_reads"] += 1
            consecutive = 0
            continue
        try:
            last = validator(frame)
        except CameraReadError as exc:
            stats["invalid_frames"] += 1
            stats["last_invalid_reason"] = str(exc)
            consecutive = 0
            continue
        received = frame
        stats["valid_frames"] += 1
        consecutive += 1
        if consecutive >= required_valid:
            return last, received, stats
    expected = (f"{descriptor.width} × {descriptor.height} Bayer uint8, {RAW_BYTES} octets"
                if descriptor.raw else f"{descriptor.width} × {descriptor.height} BGR uint8")
    detail = stats["last_invalid_reason"] or stats["last_read_error"] or "aucune image reçue"
    raise CameraStabilizationError(
        f"Images conformes au mode « {descriptor.label} » ({expected}) non obtenues : "
        f"{stats['attempts']} tentatives, {stats['valid_frames']} valides, "
        f"{stats['invalid_frames']} non conformes, {stats['failed_reads']} lectures échouées "
        f"({required_valid} images valides consécutives requises). Dernier motif : {detail}. "
        "Aucune autre résolution ni aucun autre backend n’a été essayé.", stats)


def acquire(bank: Bank, capture_id: str, *, device: CameraDevice | None = None, opener=None, enumerator=None,
            required_valid: int = REQUIRED_VALID_FRAMES,
            max_attempts: int = MAX_STABILIZATION_ATTEMPTS) -> Acquisition:
    bank.validate()
    if bank.focus is None:
        raise CameraSetupError("La banque ne définit pas de Focus manuel.")
    if bank.camera is None:
        raise CameraSelectionError("La banque ne désigne aucune caméra : sélectionnez-la dans Preview.")
    expected = device or bank.camera
    if expected.backend != DEFAULT_BACKEND or bank.camera.identity != expected.identity:
        raise CameraSelectionError(
            f"Contexte caméra incompatible avec le backend choisi {DEFAULT_BACKEND}. "
            "Sélectionnez la caméra dans Preview puis sauvegardez la banque."
        )
    GATE.claim("capture")
    cap = None
    started = time.monotonic()
    timings: dict[str, float] = {}
    result = failure = release_error = None
    try:
        device = resolve_device(expected, list_devices(DEFAULT_BACKEND, enumerator=enumerator))
        cap = (opener or open_device)(device)
        if cap is None or not cap.isOpened():
            raise CameraOpenError(
                f"Caméra inaccessible ({device.label}, backend {device.backend}). "
                "Vérifiez le câble USB et fermez les applications qui l’utilisent."
            )
        cv2 = _cv2()
        settings = _setup(cap, bank.mode, bank.focus, cv2)
        focus = settings["focus"]
        if focus["accepted"] is not True:
            raise CameraSetupError(
                f"Le pilote a refusé le Focus manuel {bank.focus}."
                if focus["accepted"] is False else f"Erreur de commande du Focus manuel : {focus.get('error')}"
            )
        setup_done = time.monotonic()
        image, frame, stats = stabilize(cap, bank.mode, required_valid=required_valid,
                                        max_attempts=max_attempts)
        timings.update(setup_s=setup_done - started, read_s=time.monotonic() - setup_done)
        result = Acquisition(image, bank.mode, bank.focus, True, focus["readback"],
                             tuple(frame.shape), str(frame.dtype), stats, timings, device,
                             capture_id, False, settings)
    except BaseException as exc:
        failure = exc
    finally:
        if cap is not None:
            release_error = release_device(cap)
        timings["total_s"] = time.monotonic() - started
        GATE.release(release_error)
    if failure is not None:
        if release_error:
            raise CameraFaultError(f"{failure} — {release_error}") from failure
        raise failure
    result.released = release_error is None
    result.release_error = release_error
    return result
