from __future__ import annotations

import re
import smtplib
import ssl
import mimetypes
from dataclasses import dataclass
from email.header import Header
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Literal

from .config import settings
from .logging_config import logger
from .projects import project_store, current_project, DEFAULT_PROJECT


Role = Literal["safety_officer", "work_area_manager", "project_manager", "safety_director"]

ROLE_NAMES: dict[Role, str] = {
    "safety_officer": "安全员",
    "work_area_manager": "工区负责人",
    "project_manager": "项目经理",
    "safety_director": "安全总监",
}

RECIPIENT_ROLES: dict[str, tuple[Role, ...]] = {
    "general": ("safety_officer", "safety_director", "work_area_manager"),
    "major": ("project_manager", "safety_director"),
}

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LOCAL_RECIPIENT_FILE = settings.root / "config" / "notification_email_test.local.txt"


@dataclass(frozen=True)
class Recipient:
    role: Role
    name: str
    email: str

    def masked(self) -> str:
        local, domain = self.email.split("@", 1)
        if len(local) <= 2:
            local = local[:1] + "*"
        else:
            local = local[:2] + "***"
        return f"{local}@{domain}"


def _read_local_file() -> str:
    if not LOCAL_RECIPIENT_FILE.exists():
        return ""
    raw = LOCAL_RECIPIENT_FILE.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def get_recipients() -> list[Recipient]:
    emails = EMAIL_PATTERN.findall(_read_local_file())
    # 文件顺序约定：第 1 个为发送邮箱，之后 4 个依次为安全员、工区负责人、项目经理、安全总监。
    recipient_emails = emails[1:5]
    roles: tuple[Role, ...] = ("safety_officer", "work_area_manager", "project_manager", "safety_director")
    return [Recipient(role=role, name=ROLE_NAMES[role], email=email) for role, email in zip(roles, recipient_emails)]


def sender_email() -> str:
    emails = EMAIL_PATTERN.findall(_read_local_file())
    return settings.email_smtp_username or (emails[0] if emails else "")


def smtp_username() -> str:
    return settings.email_smtp_username or sender_email()


def status() -> dict:
    recipients = get_recipients()
    return {
        "recipient_file_configured": LOCAL_RECIPIENT_FILE.exists(),
        "smtp_configured": bool(
            settings.email_smtp_host
            and smtp_username()
            and settings.email_smtp_password
            and sender_email()
        ),
        "sender_configured": bool(sender_email()),
        "recipients": [{"role": item.role, "role_name": item.name, "email": item.masked()} for item in recipients],
        "missing_roles": [ROLE_NAMES[role] for role in ROLE_NAMES if role not in {item.role for item in recipients}],
    }


def resolve_recipients(severity: Literal["general", "major"], requested_roles: list[Role] | None = None, *, work_area: str | None = None) -> list[Recipient]:
    wanted = set(requested_roles or RECIPIENT_ROLES[severity])
    # Prefer enabled login accounts so the person receiving the email can open
    # the linked task with the same identity. The legacy local recipient file
    # remains a safe fallback for existing test installations.
    try:
        from .auth import auth_store
        users = [item for item in auth_store.list_users() if item["enabled"] and item["role"] in wanted and project_store.member(current_project.get(), item)]
        scoped = [item for item in users if item["role"] != "work_area_manager" or not work_area or item["work_area"] in {work_area, "全部工区"}]
        if scoped:
            return [Recipient(role=item["role"], name=item["name"], email=item["email"]) for item in scoped]
    except Exception:
        logger.exception("failed to resolve recipients from account directory")
    return [item for item in get_recipients() if item.role in wanted] if current_project.get() == DEFAULT_PROJECT else []


def send_email(*, recipients: list[Recipient], subject: str, body: str, image_path: Path | None = None, action_url: str = "") -> list[dict]:
    if not recipients:
        raise ValueError("未找到可用收件人，请检查 config/notification_email_test.local.txt")
    if not status()["smtp_configured"]:
        raise RuntimeError("邮件 SMTP 尚未配置。请在 .env 填写 EMAIL_SMTP_HOST、USERNAME 和 PASSWORD（授权码）。")

    sender = sender_email()
    result: list[dict] = []
    for recipient in recipients:
        message = EmailMessage()
        message["Subject"] = str(Header(subject, "utf-8"))
        message["From"] = formataddr((str(Header(settings.email_from_name, "utf-8")), sender))
        message["To"] = recipient.email
        message.set_content(body)
        html_body = "<div style='font-family:Arial,sans-serif;line-height:1.8;white-space:pre-line'>" + body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</div>"
        if image_path and image_path.is_file():
            html_body += "<p><img src='cid:hazard-evidence' style='max-width:640px;width:100%;height:auto;border-radius:10px' alt='隐患证据图'></p>"
        if action_url:
            html_body += f"<p><a href='{action_url}' style='display:inline-block;padding:10px 18px;background:#2869c7;color:white;text-decoration:none;border-radius:8px'>登录系统核实隐患</a></p>"
        message.add_alternative(html_body, subtype="html")
        if image_path and image_path.is_file():
            subtype = (mimetypes.guess_type(image_path.name)[0] or "image/jpeg").split("/", 1)[-1]
            message.get_payload()[-1].add_related(image_path.read_bytes(), maintype="image", subtype=subtype, cid="<hazard-evidence>", filename="hazard-evidence" + image_path.suffix.lower())
        try:
            if settings.email_smtp_use_ssl:
                with smtplib.SMTP_SSL(settings.email_smtp_host, settings.email_smtp_port, context=ssl.create_default_context(), timeout=20) as client:
                    client.login(smtp_username(), settings.email_smtp_password)
                    client.send_message(message)
            else:
                with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port, timeout=20) as client:
                    client.starttls(context=ssl.create_default_context())
                    client.login(smtp_username(), settings.email_smtp_password)
                    client.send_message(message)
            result.append({"role": recipient.role, "recipient": recipient.name, "email": recipient.masked(), "status": "sent"})
        except Exception as exc:
            logger.exception("email notification failed recipient=%s", recipient.role)
            result.append({"role": recipient.role, "recipient": recipient.name, "email": recipient.masked(), "status": "failed", "error": str(exc)})
    return result


def build_hazard_email(*, job_id: str, hazard_name: str, severity: Literal["general", "major"], work_area: str, deadline: str) -> tuple[str, str]:
    level = "重大隐患" if severity == "major" else "一般隐患"
    subject = f"【智筑云AI隐患提醒】{work_area}发现{level}"
    body = (
        f"工区：{work_area}\n"
        f"隐患：{hazard_name}\n"
        f"安全等级：{level}\n"
        f"整改期限：{deadline}\n\n"
        f"请及时核实并处理。识别任务编号：{job_id}"
    )
    return subject, body


def build_hazard_summary_email(
    *,
    job_id: str,
    hazard_names: list[str],
    severity: Literal["general", "major"],
    work_area: str,
    deadline: str,
) -> tuple[str, str]:
    """Build one dispatch message containing every confirmed hazard in a job."""
    names = [name.strip() for name in hazard_names if name and name.strip()]
    if len(names) == 1:
        return build_hazard_email(
            job_id=job_id,
            hazard_name=names[0],
            severity=severity,
            work_area=work_area,
            deadline=deadline,
        )

    level = "重大隐患" if severity == "major" else "一般隐患"
    subject = f"【智筑云AI隐患提醒】{work_area}发现{len(names)}项{level}"
    hazard_text = "\n".join(f"{index}. {name}" for index, name in enumerate(names, start=1))
    body = (
        f"工区：{work_area}\n"
        f"安全等级：{level}\n"
        f"隐患数量：{len(names)}项\n"
        f"整改期限：{deadline}\n\n"
        f"隐患清单：\n{hazard_text}\n\n"
        f"请及时核实并处理。识别任务编号：{job_id}"
    )
    return subject, body
