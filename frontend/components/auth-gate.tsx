"use client";

import { LockKeyhole, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import ConsoleApp from "@/components/console-app";
import { consoleApi, type SystemUser } from "@/lib/api";

export default function AuthGate() {
  const [user, setUser] = useState<SystemUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void consoleApi.me().then((result) => setUser(result.user)).catch(() => undefined).finally(() => setChecking(false));
  }, []);

  async function login(event: React.FormEvent) {
    event.preventDefault(); setError("");
    try { const result = await consoleApi.login(email, password); setUser(result.user); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "登录失败"); }
  }

  async function changePassword(event: React.FormEvent) {
    event.preventDefault(); setError("");
    try {
      await consoleApi.changePassword(password, newPassword);
      setUser(null); setPassword(""); setNewPassword("");
      setError("密码修改成功，请使用新密码重新登录");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "密码修改失败"); }
  }

  async function logout() {
    await consoleApi.logout().catch(() => undefined);
    setUser(null); setPassword("");
  }

  if (checking) return <div className="auth-loading">正在验证登录状态……</div>;
  if (user?.must_change_password) return <main className="auth-page"><form className="auth-card" onSubmit={changePassword}>
    <span className="auth-logo"><ShieldCheck size={28}/></span><h1>首次登录修改密码</h1><p>{user.name}，请设置至少10位的新密码，修改后重新登录。</p>
    <label><span>当前临时密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={8}/></label>
    <label><span>新密码</span><input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required minLength={10}/></label>
    {error && <div className="auth-error">{error}</div>}<button type="submit">修改密码</button>
  </form></main>;
  if (!user) return <main className="auth-page"><form className="auth-card" onSubmit={login}>
    <span className="auth-logo"><LockKeyhole size={28}/></span><h1>智筑云AI安全工作台</h1><p>请使用人员管理中创建的账号登录。首次部署请先运行管理员初始化脚本。</p>
    <label><span>邮箱账号</span><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required autoComplete="username"/></label>
    <label><span>密码</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required autoComplete="current-password"/></label>
    {error && <div className="auth-error">{error}</div>}<button type="submit">登录系统</button>
  </form></main>;
  return <ConsoleApp currentUser={user} onLogout={logout}/>;
}
