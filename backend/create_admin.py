from __future__ import annotations

import getpass

from app.auth import auth_store


def main() -> None:
    if auth_store.count_users():
        raise SystemExit("系统中已经存在账号，请登录后在人员管理中创建或重置账号。")
    email = input("管理员邮箱：").strip()
    password = getpass.getpass("初始密码（至少10位）：")
    if len(password) < 10:
        raise SystemExit("密码至少10位。")
    user, _ = auth_store.create_user(name="系统管理员", email=email, role="system_admin", work_area="全部工区", password=password)
    print(f"管理员已创建：{user['email']}。首次登录后必须修改密码。")


if __name__ == "__main__":
    main()
