from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request

from .config import settings


ROLE_NAMES = {
    "system_admin": "系统管理员",
    "safety_officer": "安全员",
    "work_area_manager": "工区负责人",
    "project_manager": "项目经理",
    "safety_director": "安全总监",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthStore:
    """Local account and server-side session store.

    Only a SHA-256 digest of the opaque browser session token is persisted.
    Passwords use Argon2id and temporary passwords are returned exactly once.
    """

    SESSION_HOURS = 12

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.data_dir / "workflow.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.hasher = PasswordHasher()
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, work_area TEXT NOT NULL, password_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            "id": value["id"], "name": value["name"], "email": value["email"],
            "role": value["role"], "role_name": ROLE_NAMES.get(value["role"], value["role"]),
            "work_area": value["work_area"], "enabled": bool(value["enabled"]),
            "must_change_password": bool(value["must_change_password"]),
        }

    def count_users(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, *, name: str, email: str, role: str, work_area: str, password: str | None = None) -> tuple[dict[str, Any], str]:
        temporary = password or secrets.token_urlsafe(12)
        ident, now = f"USR-{uuid.uuid4().hex[:12]}", now_iso()
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO users(id,name,email,role,work_area,password_hash,enabled,must_change_password,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
                    (ident, name.strip(), email.strip().lower(), role, work_area.strip(), self.hasher.hash(temporary), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该邮箱已存在") from exc
            row = db.execute("SELECT * FROM users WHERE id=?", (ident,)).fetchone()
        return self._public(row), temporary

    def ensure_bootstrap_admin(self, email: str, password: str, name: str = "系统管理员") -> bool:
        if self.count_users() or not email or not password:
            return False
        self.create_user(name=name, email=email, role="system_admin", work_area="全部工区", password=password)
        return True

    def authenticate(self, email: str, password: str) -> tuple[dict[str, Any], str, str]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if not row or not row["enabled"]:
            raise ValueError("账号或密码错误")
        try:
            self.hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError) as exc:
            raise ValueError("账号或密码错误") from exc
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(24)
        expires = (datetime.now(timezone.utc) + timedelta(hours=self.SESSION_HOURS)).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now_iso(),))
            db.execute(
                "INSERT INTO auth_sessions(id,user_id,token_hash,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (f"SES-{uuid.uuid4().hex[:12]}", row["id"], hashlib.sha256(token.encode()).hexdigest(), csrf, expires, now_iso()),
            )
        return self._public(row), token, csrf

    def session_user(self, token: str) -> tuple[dict[str, Any], str] | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute(
                "SELECT u.*,s.csrf_token FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
                (digest, now_iso()),
            ).fetchone()
        return (self._public(row), row["csrf_token"]) if row else None

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def change_password(self, user_id: str, current: str, new: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise ValueError("用户不存在")
            try:
                self.hasher.verify(row["password_hash"], current)
            except (VerifyMismatchError, InvalidHashError) as exc:
                raise ValueError("当前密码错误") from exc
            db.execute("UPDATE users SET password_hash=?,must_change_password=0,updated_at=? WHERE id=?", (self.hasher.hash(new), now_iso(), user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM users ORDER BY created_at")]

    def update_user(self, user_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {key: value for key, value in changes.items() if key in {"name", "role", "work_area", "enabled"} and value is not None}
        if not allowed:
            return self.get_user(user_id)
        if "enabled" in allowed:
            allowed["enabled"] = int(bool(allowed["enabled"]))
        assignments = ",".join(f"{key}=?" for key in allowed)
        with self._connect() as db:
            db.execute(f"UPDATE users SET {assignments},updated_at=? WHERE id=?", (*allowed.values(), now_iso(), user_id))
            if allowed.get("enabled") == 0:
                db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        return self.get_user(user_id)

    def reset_password(self, user_id: str) -> str:
        temporary = secrets.token_urlsafe(12)
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise ValueError("用户不存在")
            db.execute("UPDATE users SET password_hash=?,must_change_password=1,updated_at=? WHERE id=?", (self.hasher.hash(temporary), now_iso(), user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        return temporary

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._public(row) if row else None


auth_store = AuthStore()


def require_session(request: Request, *, roles: set[str] | None = None, csrf: bool = False) -> dict[str, Any]:
    value = auth_store.session_user(request.cookies.get("zzy_session", ""))
    if value is None:
        raise HTTPException(status_code=401, detail="请先登录")
    user, csrf_token = value
    if user["must_change_password"] and request.url.path != "/api/v1/auth/change-password":
        raise HTTPException(status_code=403, detail="首次登录必须先修改密码")
    if roles and user["role"] not in roles:
        raise HTTPException(status_code=403, detail="当前账号没有此操作权限")
    if csrf and request.headers.get("X-CSRF-Token", "") != csrf_token:
        raise HTTPException(status_code=403, detail="安全校验失败，请刷新页面后重试")
    request.state.current_user = user
    request.state.csrf_token = csrf_token
    return user
