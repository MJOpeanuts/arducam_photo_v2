import json
from dataclasses import replace

import cv2
import numpy as np
import pytest

from arducam_photo_v2 import processing
from arducam_photo_v2.camera import Acquisition
from arducam_photo_v2.config import Bank, MODES, Mode


def test_color_processing_non_destructive_and_aspect_ratio():
    original = np.full((20, 40, 3), [190, 90, 20], np.uint8)
    before = original.copy()
    assert np.array_equal(processing.process_color(original, "original"), original)
    gray = processing.process_color(original, "gray")
    reduced = processing.process_color(original, "reduce", 10)
    contrast = processing.process_color(original, "contrast", 35)
    assert gray.shape == (20, 40)
    assert reduced.shape == (5, 10, 3)
    assert not np.array_equal(contrast, original)
    assert np.array_equal(original, before)


def test_demosaic_does_not_change_bayer():
    raw = np.full((8, 10), 150, np.uint8)
    before = raw.copy()
    result = processing.raw_demosaic(raw, black_level=16)
    assert result.shape == (8, 10, 3)
    assert np.array_equal(raw, before)
    assert processing.raw_demosaic(raw, 0, reference=True).shape == result.shape


def _color_capture():
    bank = Bank.draft("Couleur", "color_720p", 412)
    pixels = np.zeros((720, 1280, 3), np.uint8)
    pixels[..., 1] = 93
    acquisition = Acquisition(pixels, "color_720p", 412, True, 410, (720, 1280, 3),
                              "uint8", 6, 0, {"read_s": .2}, "MSMF", 0, bank.id, True)
    return acquisition, bank


def test_color_archive_manifest_and_no_overwrite(tmp_path):
    acquisition, bank = _color_capture()
    archived = processing.archive_capture(acquisition, bank, tmp_path)
    manifest = json.loads(archived["manifest"].read_text(encoding="utf-8"))
    assert manifest["configuration_used"]["mode"] == bank.mode
    assert manifest["bank"]["focus"] == bank.focus
    assert manifest["camera"]["received_shape"] == [720, 1280, 3]
    assert manifest["original"]["format"] == "BGR PNG"
    assert manifest["original"]["sha256"]
    assert manifest["processing"]["durations_seconds"]["archive_total_s"] >= 0
    image, raw, _ = processing.load_source(archived["manifest"])
    assert not raw and np.array_equal(image, acquisition.original)
    original_path = archived["directory"] / manifest["original"]["path"]
    original_path.write_bytes(original_path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="taille|somme de contrôle"):
        processing.load_source(archived["manifest"])
    with pytest.raises(FileExistsError):
        processing.archive_capture(acquisition, bank, tmp_path)


def test_processing_failure_keeps_archived_original(tmp_path, monkeypatch):
    monkeypatch.setitem(processing.MODES, "raw_108mp", Mode("raw_108mp", "test", 12, 9, True))
    bank = Bank.draft("RAW", "raw_108mp", 200)
    original = np.arange(108, dtype=np.uint8).reshape(9, 12)
    acquisition = Acquisition(original, "raw_108mp", 200, True, 200, (9, 12, 1), "uint8", 6, 0,
                              {}, "MSMF", 0, bank.id, True)
    monkeypatch.setattr(processing, "raw_demosaic", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("simulated demosaic failure")))
    with pytest.raises(processing.CaptureRenderError, match="original est conservé"):
        processing.archive_capture(acquisition, bank, tmp_path)
    directory = tmp_path / bank.id
    assert (directory / "original.raw").read_bytes() == original.tobytes()
    manifest = json.loads((directory / "acquisition.json").read_text())
    assert manifest["status"] == "render_error_original_preserved"
    assert manifest["errors"][0]["stage"] == "render"


def test_treatment_saves_new_file_and_never_replaces_source(tmp_path):
    source = tmp_path / "source.png"
    cv2.imwrite(str(source), np.zeros((8, 8, 3), np.uint8))
    image = np.full((8, 8), 80, np.uint8)
    output = processing.save_processed(image, source, tmp_path / "treated.png")
    assert output.exists() and cv2.imread(str(source)).shape == (8, 8, 3)
    with pytest.raises(ValueError, match="remplacer"):
        processing.save_processed(image, source, source)
    with pytest.raises(FileExistsError):
        processing.save_processed(image, source, output)
