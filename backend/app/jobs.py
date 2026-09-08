from __future__ import annotations

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .detectors import get_shared_detector
from .config import settings
from .logging_config import logger
from .pipeline import HazardPipeline
from .storage import JobStore, job_store
from .workflow_store import workflow_store
from .projects import current_project, project_store, DEFAULT_PROJECT
from .algorithm_adapters import AlgorithmContext


class JobManager:
    def __init__(self, store: JobStore | None = None, max_workers: int = 2) -> None:
        self.store = store or job_store
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hazard-job")
        self.detector = get_shared_detector()

    def submit_file(self, source_path: Path, *, source_type: str, mode: str, work_area: str = "测试工区") -> str:
        job_id = self.store.create(mode=mode, source_type=source_type, source_name=source_path.name)
        job_dir = self.store.job_dir(job_id)
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        target = input_dir / source_path.name
        shutil.move(str(source_path), str(target))
        sample_fps = 1.0 / settings.test_stream_interval_sec if mode == "test" and source_type == "video" else None
        self.executor.submit(self._run, job_id, target, source_type, mode, sample_fps, None, work_area)
        return job_id

    def submit_camera(
        self,
        source_url: str,
        *,
        camera_id: str,
        mode: str,
        work_area: str = "测试工区",
        sample_fps: float,
        duration_sec: int,
    ) -> str:
        job_id = self.store.create(mode=mode, source_type="camera", source_name=camera_id)
        self.executor.submit(
            self._run,
            job_id,
            source_url,
            "camera",
            mode,
            sample_fps,
            duration_sec,
            work_area,
        )
        return job_id

    def run_sync(
        self,
        source_path: Path,
        *,
        source_type: str,
        mode: str,
        work_area: str = "测试工区",
    ) -> dict:
        job_id = self.store.create(mode=mode, source_type=source_type, source_name=source_path.name)
        job_dir = self.store.job_dir(job_id)
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        target = input_dir / source_path.name
        shutil.move(str(source_path), str(target))
        sample_fps = 1.0 / settings.test_stream_interval_sec if mode == "test" and source_type == "video" else None
        self._run(job_id, target, source_type, mode, sample_fps, None, work_area)
        result = self.store.result(job_id)
        if result is None:
            job = self.store.get(job_id)
            raise RuntimeError(job.error if job else "同步分析失败")
        return result

    def _run(
        self,
        job_id: str,
        source: str | Path,
        source_type: str,
        mode: str,
        sample_fps: float | None,
        duration_sec: int | None,
        work_area: str = "测试工区",
    ) -> None:
        project_token = current_project.set(project_store.owner("job", job_id) or DEFAULT_PROJECT)
        started = time.perf_counter()
        self.store.update(job_id, "running")
        try:
            pipeline = HazardPipeline(detector=self.detector)
            result = pipeline.analyze(
                job_id=job_id,
                source=source,
                source_type=source_type,
                mode=mode,
                job_dir=self.store.job_dir(job_id),
                sample_fps=sample_fps,
                duration_sec=duration_sec,
                context=AlgorithmContext(project_id=current_project.get(), work_area=work_area, task_id=job_id),
                progress_callback=lambda value: self.store.save_progress(job_id, value),
            )
            persist_started = time.perf_counter()
            # Upload and stream findings use the same server-owned ledger service.
            for frame in result.frames:
                findings = [item.model_dump(mode="json") | {"source_frame_ids": [frame.frame_id]} for item in result.findings if frame.frame_id in item.source_frame_ids]
                workflow_store.create_from_findings(
                    job_id=job_id, work_area=work_area, findings=findings,
                    source_key_prefix=f"job:{job_id}", before_image=frame.artifact_url,
                )
            result.metadata["persistence_ms"] = round((time.perf_counter() - persist_started) * 1000, 1)
            result.metadata["job_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.store.save_result(job_id, result.model_dump(mode="json"))
            self.store.save_progress(job_id, {"stage": "completed", "completed": len(result.frames), "total": len(result.frames), "elapsed_sec": round(time.perf_counter() - started, 2), "estimated_remaining_sec": 0})
            logger.info("job completed id=%s source_type=%s mode=%s", job_id, source_type, mode)
        except Exception as exc:
            logger.exception("job failed id=%s", job_id)
            self.store.update(job_id, "failed", error=str(exc))
        finally:
            current_project.reset(project_token)


job_manager = JobManager()
