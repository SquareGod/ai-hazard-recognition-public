"""Stable project ownership, independent of display names and work-area names."""
from __future__ import annotations

import sqlite3
import uuid
import json
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException
from .config import settings

DEFAULT_PROJECT = "demo-construction-project"
current_project: ContextVar[str] = ContextVar("project_id", default=DEFAULT_PROJECT)


class ProjectStore:
    def __init__(self, root: Path | None = None):
        root = root or settings.data_dir
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "projects.sqlite3"
        # SQLite backup includes committed WAL data, unlike copying a live db file.
        if not self.path.exists():
            backup = root / "backups" / "before_projects"
            backup.mkdir(parents=True, exist_ok=True)
            for source in root.glob("*.sqlite3"):
                target = backup / source.name
                if not target.exists():
                    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                        src.backup(dst)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships(project_id TEXT NOT NULL,user_id TEXT NOT NULL,PRIMARY KEY(project_id,user_id));
                CREATE TABLE IF NOT EXISTS resources(kind TEXT NOT NULL,id TEXT NOT NULL,project_id TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS source_configs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO projects VALUES(?,?)", (DEFAULT_PROJECT, "示例建设项目"))

    def save_source(self, spec):
        from dataclasses import asdict
        self.bind("source", spec.id)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO source_configs VALUES(?,?)", (spec.id, json.dumps(asdict(spec))))

    def sources(self):
        from .video_sources import VideoSourceSpec
        with self.connect() as db:
            return [VideoSourceSpec(**json.loads(row[0])) for row in db.execute("SELECT payload FROM source_configs")]

    def migrate_existing(self, root: Path):
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS migrations(name TEXT PRIMARY KEY)")
            if db.execute("SELECT 1 FROM migrations WHERE name='legacy-project'").fetchone():
                return
        tables = {"jobs.sqlite3": [("jobs", "job")], "workflow.sqlite3": [("hazard_groups", "group"), ("hazards", "hazard")], "hikvision.sqlite3": [("channels", "source")]}
        for filename, pairs in tables.items():
            path = root / filename
            if not path.exists():
                continue
            with sqlite3.connect(path) as source:
                for table, kind in pairs:
                    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    for row in source.execute(f"SELECT id FROM {table}"):
                        if self.owner(kind, row[0]) is None:
                            self.bind(kind, row[0], DEFAULT_PROJECT)
                    if table == "channels":
                        for row in source.execute("SELECT DISTINCT profile_id FROM channels"):
                            self.bind("profile", row[0], DEFAULT_PROJECT)
        for path in (root / "streams").glob("*"):
            if path.is_dir():
                self.bind("stream", path.name, DEFAULT_PROJECT)
        with self.connect() as db:
            db.execute("INSERT INTO migrations VALUES('legacy-project')")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def bind(self, kind: str, ident: str, project: str | None = None):
        project = project or current_project.get()
        with self.connect() as db:
            old = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, ident)).fetchone()
            if old and old[0] != project:
                raise HTTPException(409, "资源已经属于其他项目，不能覆盖")
            db.execute("INSERT OR IGNORE INTO resources VALUES(?,?,?)", (kind, ident, project))

    def owner(self, kind: str, ident: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT project_id FROM resources WHERE kind=? AND id=?", (kind, ident)).fetchone()
            return row[0] if row else None

    def check(self, kind: str, ident: str):
        if self.owner(kind, ident) != current_project.get():
            raise HTTPException(404, "当前项目中不存在该资源")

    def filter(self, kind: str, items: list[dict], key: str = "id") -> list[dict]:
        return [dict(item, project_id=current_project.get()) for item in items if self.owner(kind, str(item.get(key, ""))) == current_project.get()]

    def member(self, project: str, user: dict) -> bool:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                return False
            return user["role"] == "system_admin" or bool(db.execute("SELECT 1 FROM memberships WHERE project_id=? AND user_id=?", (project, user["id"])).fetchone())

    def list(self, user: dict) -> list[dict]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY rowid")]
        return [row for row in rows if self.member(row["id"], user)]

    def save(self, name: str, ident: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 100:
            raise HTTPException(422, "项目名称需要1到100个字符")
        with self.connect() as db:
            if ident:
                if db.execute("UPDATE projects SET name=? WHERE id=?", (name, ident)).rowcount == 0:
                    raise HTTPException(404, "项目不存在")
            else:
                ident = "PRJ-" + uuid.uuid4().hex[:12]
                db.execute("INSERT INTO projects VALUES(?,?)", (ident, name))
        return {"id": ident, "name": name}

    def set_member(self, project: str, user_id: str, enabled: bool):
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project,)).fetchone():
                raise HTTPException(404, "项目不存在")
            if enabled:
                db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?)", (project, user_id))
            else:
                db.execute("DELETE FROM memberships WHERE project_id=? AND user_id=?", (project, user_id))


project_store = ProjectStore()
