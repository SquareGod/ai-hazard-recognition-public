import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import LiveMonitorPage, { type MonitorPageApi } from "@/components/live-monitor-page";

vi.mock("@/components/managed-stream-player", () => ({
  default: ({ camera, onEnlarge }: { camera: { id: string; name: string }; onEnlarge?: () => void }) => (
    <div data-testid="monitor-player">
      <span>{camera.name}</span>
      {onEnlarge && <button aria-label={`放大${camera.name}`} onClick={onEnlarge}>放大</button>}
    </div>
  ),
}));

const cameras = Array.from({ length: 10 }, (_, index) => ({
  id: `cam-${index + 1}`,
  name: `摄像头 ${index + 1}`,
  work_area: "示范工区",
  status: "online" as const,
  enabled: true,
}));

function api(expiresIn = 300): MonitorPageApi {
  return {
    listMonitorCameras: vi.fn(async ({ limit, offset }) => ({
      items: cameras.slice(offset, offset + limit), total: cameras.length, limit, offset, work_areas: ["示范工区"],
    })),
    createMonitorPlaybackTicket: vi.fn(async (cameraIds: string[]) => ({
      ticket_id: `ticket-${cameraIds.join("-")}`,
      expires_in: expiresIn,
      purpose: "monitor-wall",
      cameras: cameraIds.map((id) => {
        const item = cameras.find((camera) => camera.id === id)!;
        return { ...item, webrtc: `/api/v1/media/ticket/${id}/whep`, hls: `/api/v1/media/ticket/${id}/hls/index.m3u8` };
      }),
    })),
    releaseMonitorPlaybackTicket: vi.fn(async (ticketId) => ({ ticket_id: ticketId, released: true })),
  };
}

describe("LiveMonitorPage", () => {
  it("每组选择至多九路，但只让第一路保持实时播放", async () => {
    const monitorApi = api();
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);

    await waitFor(() => expect(screen.getAllByTestId("monitor-player")).toHaveLength(1));
    expect(screen.getByTestId("monitor-player")).toHaveTextContent("摄像头 1");
    expect(screen.getByRole("button", { name: "切换 摄像头 2 为主画面" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一组" }));
    await waitFor(() => expect(screen.getAllByTestId("monitor-player")).toHaveLength(1));
    expect(screen.getByTestId("monitor-player")).toHaveTextContent("摄像头 10");
  });

  it("点击主画面放大并关闭后返回单路主画面", async () => {
    render(<LiveMonitorPage api={api()} devices={[]} />);
    await waitFor(() => expect(screen.getAllByTestId("monitor-player")).toHaveLength(1));

    fireEvent.click(screen.getByRole("button", { name: "放大摄像头 1" }));
    expect(screen.getByRole("dialog", { name: "摄像头 1放大画面" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭放大画面" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("monitor-player")).toHaveLength(1);
  });

  it("工区筛选项来自后端摄像头目录", async () => {
    render(<LiveMonitorPage api={api()} devices={[]} />);
    expect(await screen.findByRole("option", { name: "示范工区" })).toBeInTheDocument();
  });

  it("可任意选择多路，应用选择后只为主画面创建播放票据", async () => {
    const monitorApi = api();
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);
    await waitFor(() => expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(1));

    fireEvent.click(await screen.findByRole("checkbox", { name: "摄像头 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /摄像头 5/ }));
    expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "应用选择" }));
    await waitFor(() => expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenLastCalledWith(["cam-1"], "monitor-wall"));
    expect(screen.queryByRole("button", { name: "上一组" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "切回分页监看" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换 摄像头 5 为主画面" }));
    await waitFor(() => expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenLastCalledWith(["cam-5"], "monitor-wall"));
  });

  it("不会自动启动 AI，只在对应摄像头点击启动后创建任务并默认发送邮件", async () => {
    const monitorApi: MonitorPageApi = {
      ...api(),
      listStreamSessions: vi.fn(async () => []),
      getStreamEvents: vi.fn(async () => []),
      startStreamSession: vi.fn(async (payload) => ({ session_id: "session-1", source_id: payload.source_id, inference_stream_id: "stream-1", status: "running" as const, started_at: 1, playback_urls: { webrtc: "", hls: "" } })),
      stopStreamSession: vi.fn(async () => ({} as never)),
    };
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);

    await screen.findAllByRole("button", { name: "启动 AI" });
    expect(monitorApi.startStreamSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "启动 AI" })[0]);
    await waitFor(() => expect(monitorApi.startStreamSession).toHaveBeenCalledWith(expect.objectContaining({ source_id: "cam-1", auto_email: true })));
  });

  it("启动 AI 使用识别后发送邮件复选框的值", async () => {
    const monitorApi: MonitorPageApi = {
      ...api(),
      startStreamSession: vi.fn(async (payload) => ({ session_id: "session-1", source_id: payload.source_id, inference_stream_id: "stream-1", status: "running" as const, started_at: 1, playback_urls: { webrtc: "", hls: "" } })),
      stopStreamSession: vi.fn(async () => ({} as never)),
    };
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);

    fireEvent.click(await screen.findByRole("checkbox", { name: /识别后发送邮件/ }));
    fireEvent.click((await screen.findAllByRole("button", { name: "启动 AI" }))[0]);
    await waitFor(() => expect(monitorApi.startStreamSession).toHaveBeenCalledWith(expect.objectContaining({ auto_email: false })));
  });

  it("切页和卸载只释放监看票据", async () => {
    const monitorApi = api();
    const view = render(<LiveMonitorPage api={monitorApi} devices={[]} />);
    await waitFor(() => expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(1));
    const firstTicket = vi.mocked(monitorApi.createMonitorPlaybackTicket).mock.results[0];
    const first = await firstTicket.value;

    fireEvent.click(screen.getByRole("button", { name: "下一组" }));
    await waitFor(() => expect(monitorApi.releaseMonitorPlaybackTicket).toHaveBeenCalledWith(first.ticket_id));
    await waitFor(() => expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(2));
    const second = await vi.mocked(monitorApi.createMonitorPlaybackTicket).mock.results[1].value;
    view.unmount();
    await waitFor(() => expect(monitorApi.releaseMonitorPlaybackTicket).toHaveBeenCalledWith(second.ticket_id));
  });

  it("在短期票据过期前自动续签", async () => {
    vi.useFakeTimers();
    const monitorApi = api(31);
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(1);

    await act(async () => { vi.advanceTimersByTime(15_000); await Promise.resolve(); await Promise.resolve(); });
    expect(monitorApi.createMonitorPlaybackTicket).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("续签临时失败时保留旧画面并继续重试", async () => {
    vi.useFakeTimers();
    const monitorApi = api(31);
    const create = vi.mocked(monitorApi.createMonitorPlaybackTicket);
    const original = create.getMockImplementation()!;
    create.mockImplementationOnce(original).mockRejectedValueOnce(new Error("temporary")).mockImplementation(original);
    render(<LiveMonitorPage api={monitorApi} devices={[]} />);
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    await act(async () => { vi.advanceTimersByTime(15_000); await Promise.resolve(); await Promise.resolve(); });
    expect(create).toHaveBeenCalledTimes(2);
    expect(screen.getAllByTestId("monitor-player")).toHaveLength(1);

    await act(async () => { vi.advanceTimersByTime(5_000); await Promise.resolve(); await Promise.resolve(); });
    expect(create).toHaveBeenCalledTimes(3);
    expect(monitorApi.releaseMonitorPlaybackTicket).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
