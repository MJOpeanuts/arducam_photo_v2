import hashlib
import json

import cv2
import numpy as np
import pytest

from arducam_photo_v2 import processing
from arducam_photo_v2.camera import Acquisition
from arducam_photo_v2.config import Bank, CameraDevice, Mode

DEVICE = CameraDevice("MSMF", 0, "Arducam B0494C", "usb#arducam")
STATS = {"required_valid": 6, "max_attempts": 30, "attempts": 8, "failed_reads": 1,
         "invalid_frames": 1, "valid_frames": 6, "last_read_error": None, "last_invalid_reason": "transitoire"}


@pytest.fixture
def synthetic_ccm(tmp_path, monkeypatch):
    """Synthetic test matrices, not manufacturer calibration data."""
    data = {"ccms": [
        {"ct": 3000, "ccm": [1.1, 0, 0, 0, 1, 0, 0, 0, .9]},
        {"ct": 5000, "ccm": [.9, 0, 0, 0, 1, 0, 0, 0, 1.1]},
    ]}
    (tmp_path / "arducam_108mp.json").write_text(json.dumps(data))
    monkeypatch.setattr(processing, "project_root", lambda: tmp_path)
    return data


def test_display_reduction_is_non_destructive_and_preserves_aspect_ratio():
    original = np.full((20, 40, 3), [190, 90, 20], np.uint8)
    before = original.copy()
    assert np.array_equal(processing.reduce_image(original, 100), original)
    reduced = processing.reduce_image(original, 10)
    assert reduced.shape == (5, 10, 3)
    assert np.array_equal(original, before)


def test_demosaic_does_not_change_bayer(synthetic_ccm):
    raw = np.full((8, 10), 150, np.uint8)
    before = raw.copy()
    result = processing.raw_demosaic(raw, black_level=16)
    assert result.shape == (8, 10, 3)
    assert np.array_equal(raw, before)
    assert processing.raw_demosaic(raw, 0, reference=True).shape == result.shape


def test_bayer_gr2rgb_output_order_matches_bgr_viewers():
    raw = np.tile(np.array([[100, 220], [20, 100]], dtype=np.uint8), (4, 4))
    result = processing.raw_demosaic(raw, black_level=0)
    assert result[1, 1].tolist() == [20, 100, 220]


def test_full_108mp_automatic_render_preserves_dimensions(synthetic_ccm):
    raw = np.full((9000, 12000), 120, np.uint8)
    result = processing.raw_demosaic(raw, reference=True)
    assert result.shape == (9000, 12000, 3) and result.dtype == np.uint8
    assert raw.shape == (9000, 12000) and raw[0, 0] == 120


def test_portable_ccm_is_resolved_beside_executable_not_current_directory(tmp_path, monkeypatch):
    executable = tmp_path / "portable" / "ArducamPhotoV2.exe"
    monkeypatch.setattr(processing.sys, "frozen", True, raising=False)
    monkeypatch.setattr(processing.sys, "executable", str(executable))
    monkeypatch.chdir(tmp_path)
    assert processing.correction_file() == executable.parent / "arducam_108mp.json"


def test_constructor_ccms_load_from_project_root_and_are_interpolated(monkeypatch, tmp_path, synthetic_ccm):
    monkeypatch.chdir(tmp_path)
    ccms, fingerprint = processing.load_ccm_config()
    assert processing.correction_file() == processing.project_root() / "arducam_108mp.json"
    assert [temperature for temperature, _ in ccms] == [3000, 5000]
    assert len(fingerprint) == 64
    expected = .5 * ccms[0][1] + .5 * ccms[1][1]
    assert np.allclose(processing.select_ccm(ccms, 4000), expected)

    raw = np.tile(np.arange(24, dtype=np.uint8), (12, 1)) + 32
    rendered = processing.raw_demosaic(raw, reference=True, ccm_config=(ccms, fingerprint))
    base = cv2.cvtColor(cv2.subtract(raw, 16), cv2.COLOR_BayerGR2RGB)
    matrix = np.rot90(processing._YUV_TO_RGB @ processing._RGB_TO_YUV @ expected, 2).T
    demo_result = np.clip(
        base.reshape(-1, 3).astype(np.float64) @ matrix,
        0, 255,
    ).astype(np.uint8).reshape(base.shape)
    assert np.array_equal(rendered, demo_result)
    assert not np.array_equal(rendered, processing.raw_demosaic(raw, 16))


def test_invalid_or_missing_constructor_ccm_is_explicit(tmp_path):
    invalid = tmp_path / "arducam_108mp.json"
    invalid.write_text('{"ccms":[{"ct":4000,"ccm":[1,2]}]}', encoding="utf-8")
    with pytest.raises(processing.CorrectionConfigError, match="9 nombres finis"):
        processing.load_ccm_config(invalid)
    with pytest.raises(processing.CorrectionConfigError, match="absent ou illisible"):
        processing.load_ccm_config(tmp_path / "missing.json")


def _color_capture():
    bank = Bank.draft("Couleur", "color_720p", 412, DEVICE)
    pixels = np.zeros((720, 1280, 3), np.uint8)
    pixels[..., 1] = 93
    acquisition = Acquisition(pixels, "color_720p", 412, True, 410, (720, 1280, 3),
                              "uint8", STATS, {"read_s": .2}, DEVICE, bank.id, True)
    return acquisition, bank


def test_color_archive_manifest_and_no_overwrite(tmp_path):
    acquisition, bank = _color_capture()
    archived = processing.archive_capture(acquisition, bank, tmp_path / "photos été 漢字")
    manifest = json.loads(archived["manifest"].read_text(encoding="utf-8"))
    assert manifest["configuration_used"]["mode"] == bank.mode
    assert archived["image"].stem == archived["manifest"].stem == acquisition.capture_id
    assert manifest["schema_version"] == 2
    assert manifest["object"] == {"id": None, "description": None}
    assert manifest["configuration_used"]["mode_label"] == processing.MODES[bank.mode].label
    assert manifest["configuration_used"]["requested_focus"] == bank.focus
    assert manifest["bank"]["focus"] == bank.focus
    assert manifest["bank"]["name"] == bank.name and manifest["bank"]["id"] == bank.id
    assert manifest["camera"]["received_shape"] == [720, 1280, 3]
    assert manifest["camera"]["stabilization"] == STATS
    assert manifest["configuration_used"]["camera"]["path"] == "usb#arducam"
    assert manifest["original"]["format"] == "BGR PNG"
    assert manifest["original"]["sha256"]
    assert manifest["processing"]["durations_seconds"]["archive_total_s"] >= 0
    original_path = archived["directory"] / manifest["original"]["path"]
    image = cv2.imdecode(np.frombuffer(original_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(image, acquisition.original)
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == manifest["original"]["sha256"]
    with pytest.raises(FileExistsError):
        processing.archive_capture(acquisition, bank, tmp_path / "photos été 漢字")


def test_archive_rejects_parameters_not_used_from_frozen_bank(tmp_path):
    acquisition, bank = _color_capture()
    different = Bank.draft(bank.name, "color_4k", bank.focus, DEVICE)
    with pytest.raises(ValueError, match="ne correspondent pas"):
        processing.archive_capture(acquisition, different, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_processing_failure_keeps_archived_original(tmp_path, monkeypatch, synthetic_ccm):
    monkeypatch.setitem(processing.MODES, "raw_108mp", Mode("raw_108mp", "test", 12, 9, True))
    bank = Bank.draft("RAW", "raw_108mp", 200, DEVICE)
    original = np.arange(108, dtype=np.uint8).reshape(9, 12)
    acquisition = Acquisition(original, "raw_108mp", 200, True, 200, (9, 12, 1), "uint8", STATS,
                              {}, DEVICE, bank.id, True)
    monkeypatch.setattr(processing, "raw_demosaic", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("simulated demosaic failure")))
    with pytest.raises(processing.CaptureRenderError, match="original est conservé"):
        processing.archive_capture(acquisition, bank, tmp_path)
    directory = tmp_path / bank.id
    assert (directory / f"{bank.id}.raw").read_bytes() == original.tobytes()
    manifest = json.loads((directory / f"{bank.id}.json").read_text())
    assert manifest["status"] == "render_error_original_preserved"
    assert manifest["errors"][0]["stage"] == "render"


def test_raw_archive_records_ccm_and_reproduces_automatic_render(tmp_path, monkeypatch, synthetic_ccm):
    monkeypatch.setitem(processing.MODES, "raw_108mp", Mode("raw_108mp", "test", 12, 9, True))
    preview_device = CameraDevice("DSHOW", DEVICE.index, DEVICE.name, DEVICE.path)
    bank = Bank.draft("RAW", "raw_108mp", 200, preview_device)
    original = np.tile(np.arange(12, dtype=np.uint8), (9, 1)) + 32
    acquisition = Acquisition(original, "raw_108mp", 200, True, 200, (9, 12, 2), "uint8", STATS,
                              {}, DEVICE, bank.id, True)
    archived = processing.archive_capture(acquisition, bank, tmp_path / "photos été")
    assert archived["image"].stem == archived["manifest"].stem == acquisition.capture_id
    assert (archived["directory"] / f"{acquisition.capture_id}.raw").exists()
    manifest = json.loads(archived["manifest"].read_text(encoding="utf-8"))
    recipe = manifest["processing"]["reference_recipe"]
    _, fingerprint = processing.load_ccm_config()
    assert recipe["ccm_temperature_kelvin"] == 4000
    assert recipe["ccm_sha256"] == fingerprint
    assert manifest["processing"]["steps"] == processing.REFERENCE_RECIPE
    original_path = archived["directory"] / manifest["original"]["path"]
    loaded = np.frombuffer(original_path.read_bytes(), np.uint8).reshape(9, 12)
    assert np.array_equal(loaded, original)
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == manifest["original"]["sha256"]
    rerendered = processing.raw_demosaic(loaded, reference=True)
    rendered = cv2.imdecode(np.frombuffer(archived["image"].read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(rerendered, rendered)


@pytest.mark.parametrize("invalid", [False, True])
def test_missing_ccm_preserves_raw_and_reports_render_failure(tmp_path, monkeypatch, invalid):
    monkeypatch.setitem(processing.MODES, "raw_108mp", Mode("raw_108mp", "test", 12, 9, True))
    project = tmp_path / "calibration"
    monkeypatch.setattr(processing, "project_root", lambda: project)
    if invalid:
        project.mkdir()
        (project / "arducam_108mp.json").write_text('{"ccms":[]}')
    bank = Bank.draft("RAW", "raw_108mp", 200, DEVICE)
    original = np.arange(108, dtype=np.uint8).reshape(9, 12)
    acquisition = Acquisition(original, "raw_108mp", 200, True, 200, (9, 12, 2), "uint8", STATS,
                              {}, DEVICE, bank.id, True)
    with pytest.raises(processing.CaptureRenderError, match="original est conservé"):
        processing.archive_capture(acquisition, bank, tmp_path / "photos")
    archive = tmp_path / "photos" / bank.id
    assert (archive / f"{bank.id}.raw").read_bytes() == original.tobytes()
    manifest = json.loads((archive / f"{bank.id}.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "render_error_original_preserved"
    assert manifest["processing"]["reference_recipe"]["ccm_sha256"] is None
    assert manifest["errors"][0]["stage"] == "render"


@pytest.mark.parametrize("maximum", [0, -1, True])
def test_display_reduction_rejects_invalid_size(maximum):
    with pytest.raises(ValueError, match="positive"):
        processing.reduce_image(np.zeros((8, 8, 3), np.uint8), maximum)
