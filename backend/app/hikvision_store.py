from __future__ import annotations
import sqlite3
from typing import Any
from .config import settings
from .video_sources import VideoSourceSpec

class HikvisionStore:
    def __init__(self) -> None:
        self.path = settings.data_dir / "hikvision.sqlite3"
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, channel_no INTEGER NOT NULL, name TEXT NOT NULL, nvr_name TEXT NOT NULL, online INTEGER NOT NULL, work_area TEXT NOT NULL, risk_point TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0, ai_enabled INTEGER NOT NULL DEFAULT 0)")
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path); db.row_factory = sqlite3.Row; return db
    def upsert_channels(self, profile_id: str, channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._connect() as db:
            for item in channels:
                no = int(item["channel_no"]); display_no = int(item.get("display_no") or no)
                ident = f"hik-{profile_id[:8]}-{display_no}"; original = str(item.get("name") or f"IPCamera{display_no}")
                db.execute("INSERT INTO channels(id,profile_id,channel_no,name,nvr_name,online,work_area) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET channel_no=excluded.channel_no,online=excluded.online,nvr_name=excluded.nvr_name", (ident, profile_id, no, original, original, int(bool(item.get("online"))), "未分配工区"))
        return self.list()
    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db: rows = db.execute("SELECT * FROM channels ORDER BY profile_id,channel_no").fetchall()
        return [self._public(dict(row)) for row in rows]
    def update(self, channel_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {k:v for k,v in changes.items() if k in {"name","work_area","risk_point","enabled","ai_enabled"} and v is not None}
        if allowed:
            assignments = ", ".join(f"{k}=?" for k in allowed); values = [int(v) if isinstance(v,bool) else v for v in allowed.values()] + [channel_id]
            with self._connect() as db: db.execute(f"UPDATE channels SET {assignments} WHERE id=?", values)
        return next((x for x in self.list() if x["id"] == channel_id), None)
    def specs(self) -> list[VideoSourceSpec]:
        return [VideoSourceSpec(
            x["id"], x["name"], "hikvision", f"hikvision://{x['id']}",
            x["work_area"], x["enabled"], x["risk_point"], x["ai_enabled"],
            {"profile_id": x["profile_id"], "channel_no": x["channel_no"]},
        ) for x in self.list()]
    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("online","enabled","ai_enabled"): row[key] = bool(row[key])
        return row

hikvision_store = HikvisionStore()
