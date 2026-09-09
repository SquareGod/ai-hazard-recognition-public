from __future__ import annotations

import base64
import os


def test_data_url_downscales_large_images(monkeypatch, tmp_path):
    import cv2
    import numpy as np

    from app.vlm import QwenVLMProvider

    monkeypatch.setenv("VLM_MAX_IMAGE_PX", "640")
    image = np.zeros((2000, 3000, 3), dtype=np.uint8)
    path = tmp_path / "big.jpg"
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    path.write_bytes(buffer.tobytes())

    url = QwenVLMProvider._data_url(path)

    assert url.startswith("data:image/jpeg;base64,")
    payload = base64.b64decode(url.split(",", 1)[1])
    decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert max(decoded.shape[:2]) <= 640


def test_data_url_keeps_small_images_untouched(monkeypatch, tmp_path):
    import cv2
    import numpy as np

    from app.vlm import QwenVLMProvider

    monkeypatch.setenv("VLM_MAX_IMAGE_PX", "1280")
    image = np.zeros((300, 400, 3), dtype=np.uint8)
    path = tmp_path / "small.jpg"
    ok, buffer = cv2.imencode(".jpg", image)
    assert ok
    path.write_bytes(buffer.tobytes())
    original = path.read_bytes()

    url = QwenVLMProvider._data_url(path)

    payload = base64.b64decode(url.split(",", 1)[1])
    assert payload == original  # 小于上限的图片不改字节，保留原始质量


def test_data_url_honors_disabled_downscale(monkeypatch, tmp_path):
    import cv2
    import numpy as np

    from app.vlm import QwenVLMProvider

    monkeypatch.setenv("VLM_MAX_IMAGE_PX", "0")
    image = np.zeros((2000, 3000, 3), dtype=np.uint8)
    path = tmp_path / "big.png"
    ok, buffer = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buffer.tobytes())
    original = path.read_bytes()

    url = QwenVLMProvider._data_url(path)

    payload = base64.b64decode(url.split(",", 1)[1])
    assert payload == original  # 0=禁用降采样，保持原图直传
    assert url.startswith("data:image/png;base64,")
    assert not os.environ.get("VLM_MAX_IMAGE_PX") == ""
