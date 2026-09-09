import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  health: vi.fn(),
  createVideoSource: vi.fn(),
  startStreamSession: vi.fn(),
  createMonitorPlaybackTicket: vi.fn(),
  releaseMonitorPlaybackTicket: vi.fn(),
  stopStreamSession: vi.fn(),
  getStreamEvents: vi.fn(),
  listVideoSources: vi.fn(),
  listHikvisionProfiles: vi.fn(),
  listHikvisionChannels: vi.fn(),
  listStreamSessions: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, consoleApi: api };
});

import ConsoleApp from "@/components/console-app";

describe("AI实时发现的设备入口", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.health.mockResolvedValue({ ok: true, api_key_configured: true, email_notification: { smtp_configured: true, recipients: [] } });
    api.createVideoSource.mockResolvedValue({ id: "cam-a1-01" });
    api.startStreamSession.mockResolvedValue({ session_id: "session-1", source_id: "cam-a1-01", inference_stream_id: "stream-1", status: "running", started_at: 1, playback_urls: { webrtc: "direct-whep", hls: "direct-hls" } });
    api.createMonitorPlaybackTicket.mockResolvedValue({ ticket_id: "ticket-1", expires_in: 300, purpose: "ai-live-preview", cameras: [{ id: "cam-a1-01", name: "CAM-A1-01", work_area: "示范工区", status: "online", enabled: true, webrtc: "/api/v1/media/ticket-1/cam-a1-01/whep", hls: "/api/v1/media/ticket-1/cam-a1-01/hls/index.m3u8" }] });
    api.releaseMonitorPlaybackTicket.mockResolvedValue({ ticket_id: "ticket-1", released: true });
    api.stopStreamSession.mockResolvedValue({});
    api.getStreamEvents.mockResolvedValue([]);
    api.listVideoSources.mockResolvedValue([]);
    api.listHikvisionProfiles.mockResolvedValue([]);
    api.listHikvisionChannels.mockResolvedValue([]);
    api.listStreamSessions.mockResolvedValue([]);
  });

  it("不在上传页暴露 RTSP 输入，并引导到集中设备和监控入口", async () => {
    render(<ConsoleApp />);
    fireEvent.click(screen.getByRole("button", { name: /AI 实时发现/ }));
    expect(screen.queryByLabelText("视频流地址")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /管理设备/ }));
    expect(screen.getByRole("heading", { name: /摄像头、无人机、机器狗/ })).toBeInTheDocument();
  });
});
