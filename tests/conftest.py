import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from arducam_photo_v2.camera import GATE  # noqa: E402


@pytest.fixture(autouse=True)
def clean_camera_gate():
    GATE.reset_for_tests()
    yield
    GATE.reset_for_tests()
