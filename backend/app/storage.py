from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .schemas import JobView
from .projects import project_store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self) -> None:
        self.db_path = settings.data_dir / "jobs.sqlite3"
        self.jobs_dir = settings.data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    result_path TEXT
                )
                """
            )

    def create(self, *, mode: str, source_type: str, source_name: str) -> str:
        job_id = uuid.uuid4().hex
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, "queued", mode, source_type, source_name, timestamp, timestamp, None, None),
            )
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        project_store.bind("job", job_id)
        return job_id

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def update(self, job_id: str, status: str, *, error: str | None = None, result_path: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, updated_at=?, error=?, result_path=COALESCE(?, result_path) WHERE id=?",
                (status, now_iso(), error, result_path, job_id),
            )

    def save_result(self, job_id: str, result: dict[str, Any]) -> Path:
        path = self.job_dir(job_id) / "result.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.update(job_id, "completed", result_path=str(path))
        return path

    def save_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        path = self.job_dir(job_id) / "progress.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def progress(self, job_id: str) -> dict:
        path = self.job_dir(job_id) / "progress.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, job_id: str) -> JobView | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobView(
            id=row["id"],
            status=row["status"],
            mode=row["mode"],
            source_type=row["source_type"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            result_url=f"/api/v1/jobs/{job_id}/result" if row["result_path"] else None,
            progress=self.progress(job_id),
        )

    def result(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT result_path FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or not row["result_path"]:
            return None
        path = Path(row["result_path"])
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def list(self, limit: int = 50) -> list[JobView]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [
            JobView(
                id=row["id"],
                status=row["status"],
                mode=row["mode"],
                source_type=row["source_type"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                error=row["error"],
                result_url=f"/api/v1/jobs/{row['id']}/result" if row["result_path"] else None,
            )
            for row in rows
        ]


job_store = JobStore()

