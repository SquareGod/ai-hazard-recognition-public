import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "智筑云 AI 安全隐患识别控制台",
  description: "面向施工现场的 AI 隐患发现、核实、分发、整改与复核闭环工作台。",
  icons: { icon: "/logo.png", shortcut: "/logo.png" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
