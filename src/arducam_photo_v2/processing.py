from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .camera import Acquisition
from .config import Bank, MODES

_RGB_TO_YUV = np.array([[0.299, 0.587, 0.114], [-0.169, -0.331, 0.500], [0.500, -0.419, -0.081]])
_YUV_TO_RGB = np.array([[1.0, 0.0, 1.402], [1.0, -0.345, -0.714], [1.0, 1.771, 0.0]])
REFERENCE_CCT = 4000
RAW_IMAGE_SHAPE = (9000, 12000)
REFERENCE_RECIPE = [
    "soustraction saturante du niveau noir 16",
    "dématriçage BayerGR2RGB",
    "CCM constructeur interpolée à 4000 K, saturation 1.0",
]


class CorrectionConfigError(ValueError):
    pass


def project_root() -> Path:
    """Resolve the source checkout independently of the process working directory."""
    return Path(__file__).resolve().parents[2]


def correction_file() -> Path:
    return project_root() / "arducam_108mp.json"


def _finite_coefficient(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def load_ccm_config(path: str | Path | None = None) -> tuple[list[tuple[int, np.ndarray]], str]:
    filename = Path(path) if path is not None else correction_file()
    try:
        payload = filename.read_bytes()
    except OSError as exc:
        raise CorrectionConfigError(
            f"Fichier de correction couleur constructeur absent ou illisible : {filename} ({exc})."
        ) from exc
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorrectionConfigError(f"JSON de correction couleur invalide : {filename} ({exc}).") from exc
    ccms = data.get("ccms") if isinstance(data, dict) else None
    if not isinstance(ccms, list) or not ccms:
        raise CorrectionConfigError(f"Le JSON de correction {filename} doit contenir une liste « ccms » non vide.")
    result: list[tuple[int, np.ndarray]] = []
    previous = 0
    for index, entry in enumerate(ccms):
        if not isinstance(entry, dict):
            raise CorrectionConfigError(f"Entrée CCM {index} invalide dans {filename}.")
        ct, coefficients = entry.get("ct"), entry.get("ccm")
        if type(ct) is not int or ct <= previous:
            raise CorrectionConfigError(
                f"Les températures CCM de {filename} doivent être des entiers strictement croissants."
            )
        if (not isinstance(coefficients, list) or len(coefficients) != 9
                or any(not _finite_coefficient(value) for value in coefficients)):
            raise CorrectionConfigError(f"La matrice CCM {index} de {filename} doit contenir 9 nombres finis.")
        result.append((ct, np.asarray(coefficients, dtype=np.float64).reshape(3, 3)))
        previous = ct
    return result, hashlib.sha256(payload).hexdigest()


def select_ccm(ccms: list[tuple[int, np.ndarray]], temperature: int) -> np.ndarray:
    if type(temperature) is not int or not ccms:
        raise ValueError("Température ou liste CCM invalide.")
    if temperature <= ccms[0][0]:
        return ccms[0][1].copy()
    if temperature >= ccms[-1][0]:
        return ccms[-1][1].copy()
    for (low_ct, low), (high_ct, high) in zip(ccms, ccms[1:]):
        if high_ct >= temperature:
            fraction = (temperature - low_ct) / (high_ct - low_ct)
            return fraction * high + (1.0 - fraction) * low
    raise ValueError("Aucune matrice CCM ne couvre la température demandée.")


class CaptureRenderError(RuntimeError):
    def __init__(self, message: str, archive: dict):
        super().__init__(message)
        self.archive = archive


def raw_demosaic(raw: np.ndarray, black_level: int = 16, reference: bool = False, *,
                 ccm_config: tuple[list[tuple[int, np.ndarray]], str] | None = None) -> np.ndarray:
    if not isinstance(raw, np.ndarray) or raw.dtype != np.uint8 or raw.ndim != 2:
        raise ValueError("Le dématriçage attend un Bayer uint8 à deux dimensions.")
    if type(black_level) is not int or not 0 <= black_level <= 255:
        raise ValueError("Niveau noir invalide.")
    image = cv2.cvtColor(cv2.subtract(raw, black_level), cv2.COLOR_BayerGR2RGB)
    if reference:
        ccms, _fingerprint = ccm_config or load_ccm_config()
        ccm = select_ccm(ccms, REFERENCE_CCT)
        matrix = np.rot90(_YUV_TO_RGB @ _RGB_TO_YUV @ ccm, 2).T
        for top in range(0, image.shape[0], 256):
            chunk = image[top:top + 256]
            chunk[...] = np.clip(chunk.reshape(-1, 3).astype(np.float64) @ matrix,
                                 0, 255).astype(np.uint8).reshape(chunk.shape)
    return image


def process_color(image: np.ndarray, operation: str, value: int = 0) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("L’image couleur doit être au format BGR uint8.")
    if operation == "original":
        return image.copy()
    if operation == "gray":
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if operation == "contrast":
        if type(value) is not int or not -100 <= value <= 100:
            raise ValueError("Le contraste doit être compris entre -100 et 100.")
        factor = (259 * (value + 255)) / (255 * (259 - value))
        return np.clip(factor * (image.astype(np.float32) - 128) + 128, 0, 255).astype(np.uint8)
    if operation == "reduce":
        if type(value) is not int or value < 1:
            raise ValueError("La dimension maximale doit être positive.")
        height, width = image.shape[:2]
        ratio = min(1., value / max(width, height))
        if ratio == 1:
            return image.copy()
        return cv2.resize(image, (max(1, round(width * ratio)), max(1, round(height * ratio))),
                          interpolation=cv2.INTER_AREA)
    raise ValueError("Traitement couleur inconnu.")


def _write_new(path: Path, data: bytes) -> dict:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": path.name, "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(data).hexdigest()}


def _write_manifest(path: Path, manifest: dict, *, create: bool = False) -> None:
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def archive_capture(acquisition: Acquisition, bank: Bank, output_dir: str | Path) -> dict:
    """Archive original pixels first; a render error never removes the acquisition."""
    bank.validate()
    same_camera = bank.camera is not None and acquisition.device.identity == bank.camera.identity
    if (MODES[acquisition.mode].raw and bank.camera is not None
            and acquisition.device.backend != bank.camera.backend):
        same_camera = (
            bool(acquisition.device.path and bank.camera.path
                 and acquisition.device.path == bank.camera.path)
            or bool(acquisition.device.name and bank.camera.name
                    and acquisition.device.name == bank.camera.name)
        )
    if (acquisition.mode != bank.mode or acquisition.focus_requested != bank.focus or not same_camera):
        raise ValueError("Les paramètres acquis ne correspondent pas à la banque figée.")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    capture_dir = root / acquisition.capture_id
    capture_dir.mkdir(exist_ok=False)
    descriptor = MODES[acquisition.mode]
    image = acquisition.original
    original_started = time.monotonic()
    if descriptor.raw:
        if image.shape != (descriptor.height, descriptor.width) or image.dtype != np.uint8:
            raise ValueError(f"RAW reconstruit invalide : {image.shape}, {image.dtype}.")
        original_record = _write_new(capture_dir / "original.raw", image.tobytes(order="C"))
        original_record.update(width=descriptor.width, height=descriptor.height, dtype="uint8", format="Bayer")
    else:
        if image.shape != (descriptor.height, descriptor.width, 3) or image.dtype != np.uint8:
            raise ValueError(f"Image couleur invalide : {image.shape}, {image.dtype}.")
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise OSError("OpenCV n’a pas pu encoder la référence PNG.")
        original_record = _write_new(capture_dir / "reference.png", encoded.tobytes())
        original_record.update(width=descriptor.width, height=descriptor.height, dtype="uint8", format="BGR PNG")
    original_write_s = time.monotonic() - original_started
    manifest = {
        "schema_version": 1,
        "capture_id": acquisition.capture_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "original_archived",
        "bank": bank.snapshot(),
        "configuration_used": {
            "mode": acquisition.mode, "mode_label": descriptor.label,
            "requested_focus": acquisition.focus_requested,
            "camera": asdict(acquisition.device),
        },
        "camera": {
            "focus_requested": acquisition.focus_requested,
            "focus_command_accepted": acquisition.focus_accepted,
            "focus_readback": acquisition.focus_readback,
            "focus_readback_note": "Une relecture n’atteste pas de l’effet optique.",
            "settings": acquisition.settings,
            "received_shape": acquisition.received_shape, "dtype": acquisition.dtype,
            "stabilization": acquisition.stabilization,
            "released_before_processing": acquisition.released,
            "release_error": acquisition.release_error,
        },
        "original": original_record, "render": None,
        "processing": {
            "steps": [],
            "reference_recipe": ({
                "steps": REFERENCE_RECIPE,
                "ccm_temperature_kelvin": REFERENCE_CCT,
                "ccm_file": "arducam_108mp.json",
                "ccm_sha256": None,
                "demosaic_output_order": "BGR, as expected by OpenCV PNG encoding and the application UI",
            } if descriptor.raw else None),
            "durations_seconds": {**acquisition.timings, "original_write_s": original_write_s},
        },
        "errors": [],
    }
    if acquisition.release_error:
        manifest["errors"].append({"stage": "camera_release", "message": acquisition.release_error})
    manifest_path = capture_dir / "acquisition.json"
    manifest_started = time.monotonic()
    _write_manifest(manifest_path, manifest, create=True)
    manifest["processing"]["durations_seconds"]["initial_manifest_s"] = time.monotonic() - manifest_started
    render_started = time.monotonic()
    try:
        if descriptor.raw:
            ccm_config = load_ccm_config()
            manifest["processing"]["reference_recipe"]["ccm_sha256"] = ccm_config[1]
            render = raw_demosaic(image, 16, reference=True, ccm_config=ccm_config)
            manifest["processing"]["steps"] = REFERENCE_RECIPE.copy()
            render_name = "reference-color.png"
        else:
            render = image
            manifest["processing"]["steps"] = ["référence couleur sans perte"]
            render_name = "reference.png"
        if descriptor.raw:
            ok, encoded = cv2.imencode(".png", render)
            if not ok:
                raise OSError("OpenCV n’a pas pu encoder le rendu PNG.")
            manifest["render"] = _write_new(capture_dir / render_name, encoded.tobytes())
        else:
            manifest["render"] = original_record
        manifest["processing"]["durations_seconds"]["render_s"] = time.monotonic() - render_started
        manifest["status"] = "complete_with_camera_release_error" if acquisition.release_error else "complete"
    except Exception as exc:
        manifest["errors"].append({"stage": "render", "message": str(exc)})
        manifest["status"] = "render_error_original_preserved"
        manifest["processing"]["durations_seconds"]["render_s"] = time.monotonic() - render_started
        manifest["processing"]["durations_seconds"]["archive_total_s"] = time.monotonic() - original_started
        _write_manifest(manifest_path, manifest)
        archive = {"directory": capture_dir, "manifest": manifest_path, "image": None,
                   "original": capture_dir / original_record["path"], "raw": descriptor.raw,
                   "focus_command_accepted": acquisition.focus_accepted,
                   "focus_readback": acquisition.focus_readback,
                   "release_error": acquisition.release_error}
        raise CaptureRenderError(
            f"Rendu échoué ; l’original est conservé dans {archive['original']} "
            f"(manifeste : {manifest_path}). {exc}", archive
        ) from exc
    manifest["processing"]["durations_seconds"]["archive_total_s"] = time.monotonic() - original_started
    _write_manifest(manifest_path, manifest)
    return {"directory": capture_dir, "manifest": manifest_path, "image": capture_dir / render_name,
            "raw": descriptor.raw, "focus_requested": acquisition.focus_requested,
            "focus_command_accepted": acquisition.focus_accepted,
            "focus_readback": acquisition.focus_readback, "release_error": acquisition.release_error}


def load_source(path: str | Path) -> tuple[np.ndarray, bool, str]:
    """Read saved files only. A JSON acquisition manifest identifies its original."""
    source = Path(path)
    if source.suffix.lower() == ".json":
        manifest = json.loads(source.read_text(encoding="utf-8"))
        original = manifest["original"]
        target = (source.parent / original["path"]).resolve()
        if not target.is_relative_to(source.parent.resolve()):
            raise ValueError("Le manifeste référence un fichier hors de son dossier.")
        payload = target.read_bytes()
        if len(payload) != original["size_bytes"]:
            raise ValueError("La taille de l’original ne correspond pas au manifeste.")
        if hashlib.sha256(payload).hexdigest() != original["sha256"]:
            raise ValueError("La somme de contrôle de l’original ne correspond pas au manifeste.")
        if original["format"] == "Bayer":
            width, height = original["width"], original["height"]
            if len(payload) != width * height or (height, width) != RAW_IMAGE_SHAPE:
                raise ValueError("Dimensions du Bayer RAW incorrectes.")
            return np.frombuffer(payload, np.uint8).reshape(height, width).copy(), True, str(source)
        image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("L’image d’acquisition ne peut pas être décodée.")
        return image, False, str(source)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"Fichier illisible. Choisissez une image PNG ou JPEG enregistrée : {exc}") from exc
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Fichier illisible. Choisissez une image PNG ou JPEG enregistrée.")
    return image, False, str(source)


def save_processed(image: np.ndarray, source_path: str | Path, destination: str | Path) -> Path:
    target = Path(destination)
    if target.resolve() == Path(source_path).resolve():
        raise ValueError("Le résultat ne peut pas remplacer la source.")
    if target.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ValueError("Choisissez une extension PNG ou JPEG.")
    ok, encoded = cv2.imencode(target.suffix.lower(), image)
    if not ok:
        raise OSError("Impossible d’encoder le résultat.")
    with target.open("xb") as stream:
        stream.write(encoded.tobytes())
        stream.flush()
        os.fsync(stream.fileno())
    return target
