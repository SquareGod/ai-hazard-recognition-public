from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.detectors import load_prompt_classes


def main() -> None:
    settings.model_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("YOLO_CONFIG_DIR", str(settings.root / "runtime" / "ultralytics"))
    os.environ.setdefault("TORCH_HOME", str(settings.root / "runtime" / "torch"))
    os.chdir(settings.model_dir)
    base_ready = settings.detector_model.exists()
    if settings.detector_provider == "yolo_world":
        from ultralytics import YOLOWorld

        if not base_ready:
            YOLOWorld(settings.detector_model.name)
        classes = load_prompt_classes()
        for mode, selected in (
            ("realtime", [item for item in classes if item.get("realtime")]),
            ("inspection", classes),
        ):
            target = settings.model_dir / f"{settings.detector_model.stem}-{mode}.pt"
            if target.exists():
                print(f"预编码模型已经存在：{target}")
                continue
            print(f"正在为{mode}链路编码{len(selected)}个开放词汇类别，首次会下载CLIP文本编码器……")
            model = YOLOWorld(str(settings.detector_model))
            model.set_classes([str(item["prompt"]) for item in selected])
            model.save(str(target))
            print(f"已生成：{target}")
    else:
        if not base_ready:
            from ultralytics import YOLO

            YOLO(settings.detector_model.name)
    if not settings.detector_model.exists():
        raise RuntimeError(f"下载完成但未找到预期文件：{settings.detector_model}")
    print(f"模型已下载：{settings.detector_model}")


if __name__ == "__main__":
    main()
